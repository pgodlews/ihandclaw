"""Fake daemon HTTP server serving offline sessions as a screen graph.

Follows docs/SPEC.md (sections 6 and 8).
Serves the exact same HTTP routes as the real daemon:
- GET /: Web dashboard with screen, overlays, and action log
- GET /api/frame.jpg: Current screen frame (JPEG, supports ?scale=pt2 for 750x1624)
- GET /api/stream.mjpeg: MJPEG stream
- GET /api/elements: DroidClaw UIElement JSON (supports ?annotate=1 for marked frame)
- GET /api/state: Daemon state, BLE/link status, action log, off-path stats
- POST /api/pause: Pause/unpause actions
- POST /api/reset: Reset simulator back to initial screen
- POST /api/{action}: Executes tap/key/type/etc. against the screen graph
  Moves to next screen if action matches; stays and logs 'off-path' otherwise.
Includes token auth and origin checks matching the real daemon.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
import hmac
from io import BytesIO
import json
import logging
import math
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageDraw, ImageFont
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .graph import ScreenGraph, ScreenNode, Transition
from .manifest import parse_bounds

logger = logging.getLogger("replay.server")
PUBLIC_SIZE = (750, 1624)  # 2 px per pt for 375x812 pt iPhone XS


def is_password_screen(elements: list[dict[str, Any]]) -> bool:
    """Check if any element on the current screen is a password/passcode prompt."""
    return any(
        re.search(r"\b(password|passcode|hasło|kod)\b", str(item.get("text", "")), re.IGNORECASE)
        for item in elements
    )


def annotate_frame(image: Image.Image, elements: list[dict[str, Any]], pointer: dict[str, Any] | None = None) -> Image.Image:
    """Draw cyan bounding boxes and element labels on frame, matching SPEC section 8."""
    img = image.copy().convert("RGB")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    sx, sy = img.width / 375.0, img.height / 812.0

    for item in elements:
        bounds = item.get("bounds_pt") or parse_bounds(item.get("bounds", ""))
        l, t, r, b = bounds
        draw.rectangle((l * sx, t * sy, r * sx, b * sy), outline="cyan", width=2)
        draw.text((l * sx + 2, t * sy + 2), str(item.get("id", "")), font=font, fill="cyan")
        if "center" in item:
            cx, cy = item["center"]
            draw.ellipse((cx * sx - 3, cy * sy - 3, cx * sx + 3, cy * sy + 3), fill="cyan")

    if pointer and "x" in pointer and "y" in pointer:
        px, py = pointer["x"] * sx, pointer["y"] * sy
        rad = 16.0 * sx
        draw.ellipse((px - rad, py - rad, px + rad, py + rad), outline="yellow", width=3)

    return img


class TokenAuthMiddleware:
    """Enforces Bearer token auth and anti-CORS origin checks matching real daemon."""

    def __init__(self, app, token: str):
        if not token:
            raise ValueError("TokenAuthMiddleware requires a non-empty token")
        self.app = app
        self.token = token.encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"")
        query_string = scope.get("query_string", b"").decode("utf-8", errors="replace")
        query = parse_qs(query_string)

        supplied = b""
        if auth_header.startswith(b"Bearer "):
            supplied = auth_header[7:].strip()
        elif "token" in query and query["token"]:
            supplied = query["token"][0].encode("utf-8")

        if not hmac.compare_digest(supplied, self.token):
            resp = JSONResponse(
                {"error": "Authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            return await resp(scope, receive, send)

        # Cross-origin check
        origin = headers.get(b"origin")
        host = headers.get(b"host", b"").decode("utf-8")
        if origin:
            origin_netloc = urlsplit(origin.decode("utf-8")).netloc
            if origin_netloc != host:
                resp = JSONResponse({"error": "Cross-origin request refused"}, status_code=403)
                return await resp(scope, receive, send)

        return await self.app(scope, receive, send)


class ReplayServer:
    """Simulator daemon serving a recorded ScreenGraph."""

    ACTIONS = {
        "tap", "long_press", "drag", "scroll", "move", "type", "key", "home", "back",
        "launch", "tap_text", "tap_element", "scroll_until", "wait_for", "run_shortcut",
    }

    def __init__(
        self,
        graph: ScreenGraph,
        token: str,
        tolerance_pt: float = 15.0,
        base_dir: Path | str | None = None,
    ):
        if not token:
            raise ValueError("ReplayServer requires a non-empty token")
        self.graph = graph
        self.token = token
        self.tolerance_pt = tolerance_pt
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()

        if not self.graph.initial_screen_id or self.graph.initial_screen_id not in self.graph.nodes:
            raise ValueError("ScreenGraph has no valid initial screen node")

        self.current_screen_id = self.graph.initial_screen_id
        self.paused = False
        self.off_path_count = 0
        self.total_actions = 0
        self.matched_actions = 0
        self.action_log: deque[dict[str, Any]] = deque(maxlen=100)
        self._image_cache: dict[str, Image.Image] = {}

    @property
    def current_node(self) -> ScreenNode:
        return self.graph.nodes[self.current_screen_id]

    def reset(self) -> None:
        """Reset simulator state back to initial screen."""
        self.current_screen_id = self.graph.initial_screen_id or list(self.graph.nodes.keys())[0]
        logger.info("Simulator reset to initial screen: %s", self.current_screen_id)

    def load_image(self, node: ScreenNode) -> Image.Image:
        """Load and cache PIL image for a screen node."""
        if node.screen_id in self._image_cache:
            return self._image_cache[node.screen_id]

        path = Path(node.frame_path)
        if not path.is_absolute():
            path = self.base_dir / path

        if path.exists():
            img = Image.open(path).convert("RGB")
        else:
            # Fallback synthetic frame if image file not on disk
            img = Image.new("RGB", (1126, 2434), color=(30, 30, 40))
            draw = ImageDraw.Draw(img)
            draw.text((100, 100), f"Replay: {node.screen_id}", fill="white")

        self._image_cache[node.screen_id] = img
        return img

    def get_state(self) -> dict[str, Any]:
        node = self.current_node
        return {
            "paused": self.paused,
            "current_screen_id": self.current_screen_id,
            "off_path_count": self.off_path_count,
            "total_actions": self.total_actions,
            "matched_actions": self.matched_actions,
            "pointer": node.pointer,
            "link": {"connected": True, "kind": "replay"},
            "ble": {"conn": True, "ready": True},
            "airplay": {"mirroring": True, "fps": 60, "source": "replay_simulator"},
            "action_log": list(self.action_log),
        }

    def execute_action(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute action against the current screen node.

        Returns {'result': ..., 'state': ...}
        """
        if self.paused:
            raise RuntimeError("Actions are paused")

        if action == "type" and is_password_screen(self.current_node.elements):
            raise PermissionError("Typing refused: password prompt detected")

        self.total_actions += 1
        start_time = time.perf_counter()

        # Attempt to match action against outgoing transitions
        match = self.graph.match_action(
            self.current_screen_id, action, args, tolerance_pt=self.tolerance_pt
        )

        duration_ms = (time.perf_counter() - start_time) * 1000.0

        if match is not None:
            next_node, trans = match
            from_screen = self.current_screen_id
            self.current_screen_id = next_node.screen_id
            self.matched_actions += 1

            entry = {
                "action": action,
                "time": time.time(),
                "ok": True,
                "matched": True,
                "off_path": False,
                "from_screen": from_screen,
                "to_screen": next_node.screen_id,
                "duration_ms": duration_ms,
            }
            self.action_log.append(entry)
            logger.info("Matched action '%s': %s -> %s", action, from_screen, next_node.screen_id)
            result = trans.response_result or {"status": "ok", "moved_to": next_node.screen_id}
            return {"result": result, "state": self.get_state()}

        # Off-path: action did not match recorded transition
        self.off_path_count += 1
        entry = {
            "action": action,
            "time": time.time(),
            "ok": False,
            "matched": False,
            "off_path": True,
            "screen": self.current_screen_id,
            "duration_ms": duration_ms,
        }
        self.action_log.append(entry)
        logger.warning(
            "off-path: action '%s' with args %s did not match any recorded transition from screen '%s'",
            action, args, self.current_screen_id
        )
        result = {
            "status": "off-path",
            "matched": False,
            "message": f"Action '{action}' did not match recorded transitions from screen '{self.current_screen_id}'",
            "screen": self.current_screen_id,
        }
        return {"result": result, "state": self.get_state()}


def create_replay_app(
    graph: ScreenGraph,
    token: str,
    tolerance_pt: float = 15.0,
    base_dir: Path | str | None = None,
) -> Starlette:
    """Create Starlette ASGI application for the replay server."""
    server = ReplayServer(graph=graph, token=token, tolerance_pt=tolerance_pt, base_dir=base_dir)

    async def index(request: Request):
        node = server.current_node
        html = f"""<!DOCTYPE html>
<html>
<head>
  <title>iPhone Hand Replay Simulator</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #1a1a24; color: #e0e0e0; margin: 0; padding: 20px; }}
    .container {{ display: flex; gap: 20px; }}
    .screen-view {{ flex: 0 0 375px; background: #000; border-radius: 20px; overflow: hidden; box-shadow: 0 8px 24px rgba(0,0,0,0.5); }}
    .screen-view img {{ width: 100%; display: block; }}
    .dashboard {{ flex: 1; }}
    .card {{ background: #242434; border-radius: 10px; padding: 16px; margin-bottom: 16px; }}
    h1 {{ margin-top: 0; font-size: 20px; color: #fff; }}
    .badge {{ display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; }}
    .badge-ok {{ background: #1b5e20; color: #a5d6a7; }}
    .badge-off {{ background: #b71c1c; color: #ffcdd2; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 8px; text-align: left; border-bottom: 1px solid #33334d; }}
    button {{ background: #3f51b5; color: white; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; font-size: 13px; margin-right: 8px; }}
    button:hover {{ background: #5c6bc0; }}
  </style>
</head>
<body>
  <h1>iPhone Hand Replay Simulator (SPEC v0.2 offline)</h1>
  <div class="container">
    <div class="screen-view">
      <img id="screen" src="/api/frame.jpg?token={token}" alt="Screen Frame" />
    </div>
    <div class="dashboard">
      <div class="card">
        <h3>Simulator Status</h3>
        <p><strong>Current Screen:</strong> <code>{server.current_screen_id}</code></p>
        <p><strong>Total Actions:</strong> {server.total_actions} | <strong>Matched:</strong> {server.matched_actions} | <strong>Off-Path:</strong> {server.off_path_count}</p>
        <p><strong>Elements Count:</strong> {len(node.elements)}</p>
        <div>
          <button onclick="fetch('/api/reset', {{method:'POST', headers:{{'Authorization':'Bearer {token}'}}}}).then(()=>location.reload())">Reset to Start</button>
          <button onclick="fetch('/api/home', {{method:'POST', headers:{{'Authorization':'Bearer {token}'}}}}).then(()=>location.reload())">Home</button>
          <button onclick="location.reload()">Refresh</button>
        </div>
      </div>
      <div class="card">
        <h3>Action Log</h3>
        <table>
          <tr><th>Time</th><th>Action</th><th>Status</th><th>Details</th></tr>
          {"".join(f"<tr><td>{e.get('time', 0):.2f}</td><td>{e.get('action')}</td><td><span class='badge {'badge-ok' if e.get('ok') else 'badge-off'}'>{'MATCHED' if e.get('ok') else 'OFF-PATH'}</span></td><td>{e.get('to_screen') or e.get('screen')}</td></tr>" for e in reversed(list(server.action_log)))}
        </table>
      </div>
    </div>
  </div>
</body>
</html>"""
        return HTMLResponse(html)

    async def get_frame(request: Request):
        node = server.current_node
        scale = request.query_params.get("scale")
        if scale and scale not in ("pt2",):
            return JSONResponse({"error": "scale must be pt2"}, status_code=422)

        img = server.load_image(node)
        if scale == "pt2":
            img = img.resize(PUBLIC_SIZE, Image.Resampling.LANCZOS)

        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return Response(buf.getvalue(), media_type="image/jpeg")

    async def get_stream(request: Request):
        async def frame_generator():
            while True:
                node = server.current_node
                img = server.load_image(node).resize(PUBLIC_SIZE, Image.Resampling.LANCZOS)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=80)
                data = buf.getvalue()
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
                    + data + b"\r\n"
                )
                await asyncio.sleep(0.1)

        return StreamingResponse(frame_generator(), media_type="multipart/x-mixed-replace; boundary=frame")

    async def get_elements(request: Request):
        node = server.current_node
        annotate = request.query_params.get("annotate") in ("1", "true", "True")
        if annotate:
            img = server.load_image(node)
            annotated = annotate_frame(img, node.elements, node.pointer)
            buf = BytesIO()
            annotated.save(buf, format="JPEG", quality=85)
            return Response(buf.getvalue(), media_type="image/jpeg")

        return JSONResponse({
            "elements": node.elements,
            "pointer": node.pointer,
            "tap_offset_pt": [10.0, 12.0],
            "ocr_available": True,
            "ocr_error": None,
            "latency_ms": 1.0,
        })

    async def get_state(request: Request):
        return JSONResponse(server.get_state())

    async def pause(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict) or "paused" not in body:
                return JSONResponse({"error": "Expected {paused: boolean}"}, status_code=422)
            server.paused = bool(body["paused"])
            return JSONResponse(server.get_state())
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=422)

    async def reset_route(request: Request):
        server.reset()
        return JSONResponse({"status": "reset", "current_screen_id": server.current_screen_id})

    def make_action_endpoint(action: str):
        async def endpoint(request: Request):
            try:
                body = await request.json() if request.method == "POST" else {}
                if not isinstance(body, dict):
                    body = {}
            except Exception:
                body = {}

            try:
                res = server.execute_action(action, body)
                return JSONResponse(res)
            except RuntimeError as exc:
                return JSONResponse({"error": str(exc)}, status_code=423)
            except PermissionError as exc:
                return JSONResponse({"error": str(exc)}, status_code=409)
            except Exception as exc:
                return JSONResponse({"error": str(exc)}, status_code=500)

        return endpoint

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/api/frame.jpg", get_frame, methods=["GET"]),
        Route("/api/stream.mjpeg", get_stream, methods=["GET"]),
        Route("/api/elements", get_elements, methods=["GET"]),
        Route("/api/state", get_state, methods=["GET"]),
        Route("/api/pause", pause, methods=["POST"]),
        Route("/api/reset", reset_route, methods=["POST"]),
    ]

    for act in server.ACTIONS:
        routes.append(Route(f"/api/{act}", make_action_endpoint(act), methods=["POST"]))

    middleware = [Middleware(TokenAuthMiddleware, token=token)]
    app = Starlette(routes=routes, middleware=middleware)
    app.state.server = server
    return app


def main():
    parser = argparse.ArgumentParser(description="Run iPhone Hand replay simulator fake daemon")
    parser.add_argument("--dataset", required=True, help="Path to dataset directory or graph.json")
    parser.add_argument("--token", required=True, help="Auth token (required)")
    parser.add_argument("--port", type=int, default=8790, help="Listen port (default 8790)")
    parser.add_argument("--host", default="127.0.0.1", help="Listen host (default 127.0.0.1)")
    parser.add_argument("--tolerance", type=float, default=15.0, help="Tap tolerance in points")

    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    if dataset_path.is_file() and dataset_path.suffix == ".json":
        graph = ScreenGraph.load(dataset_path)
        base_dir = dataset_path.parent
    elif (dataset_path / "graph.json").exists():
        graph = ScreenGraph.load(dataset_path / "graph.json")
        base_dir = dataset_path
    elif (dataset_path / "manifest.json").exists():
        from .builder import DatasetBuilder
        builder = DatasetBuilder()
        graph = builder.merge_sessions([dataset_path])
        base_dir = dataset_path
    else:
        print(f"Error: Could not find graph.json or manifest.json in {dataset_path}")
        return

    app = create_replay_app(graph, token=args.token, tolerance_pt=args.tolerance, base_dir=base_dir)
    print(f"Starting iPhone Hand Replay Simulator on http://{args.host}:{args.port}")
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
