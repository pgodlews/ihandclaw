"""Client for recording live iPhone sessions via the daemon HTTP API into datasets.

Follows docs/SPEC.md (sections 6 and 8).
For every step, captures:
- Pre-action frame (JPEG)
- Pre-action elements (JSON in droidclaw UIElement format)
- The action taken and arguments
- The action response
- Post-action resulting frame (JPEG)
All written to a dataset folder with manifest.json.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .manifest import ActionRecord, SessionManifest, StepRecord


class SessionRecorder:
    """Client that records interaction steps through the daemon HTTP API into a dataset."""

    def __init__(
        self,
        daemon_url: str,
        token: str,
        output_dir: Path | str,
        session_id: str | None = None,
        scale: str | None = None,
    ):
        self.daemon_url = daemon_url.rstrip("/")
        self.token = token
        self.output_dir = Path(output_dir)
        self.scale = scale
        self.session_id = session_id or f"session_{int(time.time())}"
        self.step_count = 0

        # Create directories
        self.frames_dir = self.output_dir / "frames"
        self.elements_dir = self.output_dir / "elements"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.elements_dir.mkdir(parents=True, exist_ok=True)

        self.manifest = SessionManifest(
            version="1.0",
            session_id=self.session_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            daemon_url=self.daemon_url,
            screen_size_pt=[375, 812],
            steps=[],
        )
        self.manifest_file = self.output_dir / "manifest.json"
        self.manifest.save(self.manifest_file)

        self._headers = {"Authorization": f"Bearer {self.token}"}
        self.client = httpx.Client(timeout=30.0, headers=self._headers)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> SessionRecorder:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def fetch_frame(self, scale: str | None = None) -> bytes:
        """Fetch a JPEG frame from the daemon."""
        url = f"{self.daemon_url}/api/frame.jpg"
        s = scale or self.scale
        params = {"scale": s} if s else {}
        resp = self.client.get(url, params=params)
        resp.raise_for_status()
        return resp.content

    def fetch_elements(self) -> dict[str, Any]:
        """Fetch elements JSON from the daemon."""
        url = f"{self.daemon_url}/api/elements"
        resp = self.client.get(url)
        resp.raise_for_status()
        return resp.json()

    def fetch_state(self) -> dict[str, Any]:
        """Fetch state JSON from the daemon."""
        url = f"{self.daemon_url}/api/state"
        resp = self.client.get(url)
        resp.raise_for_status()
        return resp.json()

    def record_step(self, action: str, **action_args) -> dict[str, Any]:
        """Execute one action on the daemon and record the step to the dataset."""
        step_idx = self.step_count
        timestamp = time.time()

        # 1. Capture pre-action frame and elements
        pre_frame_bytes = self.fetch_frame()
        elements_data = self.fetch_elements()

        # 2. Execute action on live daemon
        action_url = f"{self.daemon_url}/api/{action}"
        action_resp = self.client.post(action_url, json=action_args)
        action_resp.raise_for_status()
        response_data = action_resp.json()

        # Small settle delay for AirPlay frame receiver if needed
        time.sleep(0.3)

        # 3. Capture post-action frame
        post_frame_bytes = self.fetch_frame()

        # 4. Save files
        pre_frame_rel = f"frames/frame_{step_idx:04d}_pre.jpg"
        post_frame_rel = f"frames/frame_{step_idx:04d}_post.jpg"
        elements_rel = f"elements/elements_{step_idx:04d}.json"

        (self.output_dir / pre_frame_rel).write_bytes(pre_frame_bytes)
        (self.output_dir / post_frame_rel).write_bytes(post_frame_bytes)
        (self.output_dir / elements_rel).write_text(json.dumps(elements_data, indent=2))

        # 5. Record step in manifest
        step_rec = StepRecord(
            step=step_idx,
            timestamp=timestamp,
            action=ActionRecord(name=action, args=action_args),
            pre_frame=pre_frame_rel,
            elements_file=elements_rel,
            post_frame=post_frame_rel,
            response=response_data,
        )
        self.manifest.steps.append(step_rec)
        self.manifest.save(self.manifest_file)

        self.step_count += 1
        return response_data

    def create_proxy_app(self) -> Starlette:
        """Create a transparent recording proxy ASGI app.

        Agents point to this proxy instead of the real daemon.
        GET requests are forwarded directly; POST /api/{action} requests
        are recorded to the dataset with pre/post frames.
        """
        async def proxy_get(request: Request):
            path = request.url.path
            query = str(request.url.query)
            target = f"{self.daemon_url}{path}" + (f"?{query}" if query else "")
            async with httpx.AsyncClient(timeout=30.0, headers=self._headers) as async_client:
                resp = await async_client.get(target)
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    media_type=resp.headers.get("content-type"),
                )

        async def proxy_action(request: Request):
            action = request.path_params.get("action", "")
            try:
                body = await request.json()
            except Exception:
                body = {}

            # Execute via recorder step
            try:
                result = self.record_step(action, **body)
                return JSONResponse(result)
            except httpx.HTTPStatusError as exc:
                return JSONResponse(
                    {"error": f"Daemon returned {exc.response.status_code}: {exc.response.text}"},
                    status_code=exc.response.status_code,
                )
            except Exception as exc:
                return JSONResponse({"error": str(exc)}, status_code=500)

        routes = [
            Route("/api/frame.jpg", proxy_get, methods=["GET"]),
            Route("/api/elements", proxy_get, methods=["GET"]),
            Route("/api/state", proxy_get, methods=["GET"]),
            Route("/api/{action}", proxy_action, methods=["POST"]),
        ]
        return Starlette(routes=routes)


def main():
    parser = argparse.ArgumentParser(description="Record a live iPhone session via daemon HTTP API")
    parser.add_argument("--daemon", default="http://iphone-hand.local:8790", help="Daemon base URL")
    parser.add_argument("--token", required=True, help="Daemon auth token")
    parser.add_argument("--out", required=True, help="Output dataset directory")
    parser.add_argument("--proxy-port", type=int, default=None, help="Run as recording proxy on port")
    parser.add_argument("--action", help="Single action to record (e.g. tap, key, type, home)")
    parser.add_argument("--args", default="{}", help="JSON action arguments")

    args = parser.parse_args()
    recorder = SessionRecorder(daemon_url=args.daemon, token=args.token, output_dir=args.out)

    if args.proxy_port:
        import uvicorn
        print(f"Starting recording proxy on port {args.proxy_port} -> {args.daemon}")
        uvicorn.run(recorder.create_proxy_app(), host="127.0.0.1", port=args.proxy_port)
        return

    if args.action:
        action_args = json.loads(args.args)
        res = recorder.record_step(args.action, **action_args)
        print(f"Recorded step: {res}")
        return

    print(f"Session recorder initialized at {args.out}")


if __name__ == "__main__":
    main()
