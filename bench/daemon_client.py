"""HTTP client for the iphone-hand daemon API (SPEC sections 6 and 8).

Uses Python's standard library (urllib.request) to avoid third-party network dependencies.
"""

from __future__ import annotations
import io
import json
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
from PIL import Image

# SPEC reference screen (iPhone XS). Daemons report the actual size in GET /api/state.
REFERENCE_PT = (375, 812)


class DaemonError(RuntimeError):
    """Raised when daemon responds with an HTTP error or unreachable."""
    pass


class DaemonClient:
    def __init__(
        self,
        base_url: str = "http://iphone-hand.local:8790",
        token: str | None = None,
        timeout: float = 10.0,
        fake_daemon: Any | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.fake_daemon = fake_daemon

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "iphone-hand-bench/0.1"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        # If in-process fake daemon is attached, short-circuit network directly
        if self.fake_daemon is not None:
            return self.fake_daemon.handle_in_process(method, path, body)

        url = f"{self.base_url}{path}"
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                content_type = resp.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    return json.loads(raw.decode("utf-8"))
                return raw
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise DaemonError(f"HTTP {e.code} from daemon {path}: {err_body}") from e
        except urllib.error.URLError as e:
            raise DaemonError(f"Failed to connect to daemon at {url}: {e.reason}") from e

    def get_state(self) -> dict[str, Any]:
        return self._request("GET", "/api/state")

    def screen_pt(self) -> tuple[int, int]:
        """Screen size in points from /api/state ("screen": {"pt": [w, h]}).

        A daemon without the field (older daemons, other simulators) is the 375x812 reference.
        """
        state = self.get_state()
        pt = (state.get("screen") or {}).get("pt") if isinstance(state, dict) else None
        if (isinstance(pt, (list, tuple)) and len(pt) == 2 and
                all(type(v) is int and v > 0 for v in pt)):
            return int(pt[0]), int(pt[1])
        return REFERENCE_PT

    def get_elements(self, annotate: bool = False) -> list[dict[str, Any]]:
        query = "?annotate=1" if annotate else ""
        res = self._request("GET", f"/api/elements{query}")
        if isinstance(res, list):
            return res
        if isinstance(res, dict) and "elements" in res:
            return res["elements"]
        return []

    def get_frame(self, scale: str | None = None) -> Image.Image:
        if self.fake_daemon is not None:
            return self.fake_daemon.get_frame_image(scale=scale)

        query = f"?scale={scale}" if scale else ""
        raw_bytes = self._request("GET", f"/api/frame.jpg{query}")
        return Image.open(io.BytesIO(raw_bytes))

    def home(self) -> dict[str, Any]:
        return self._request("POST", "/api/home", {})

    def back(self) -> dict[str, Any]:
        return self._request("POST", "/api/back", {})

    def launch(self, app: str) -> dict[str, Any]:
        return self._request("POST", "/api/launch", {"app": app})

    def tap(self, x: float, y: float, button: str = "left") -> dict[str, Any]:
        return self._request("POST", "/api/tap", {"x": x, "y": y, "button": button})

    def tap_text(self, text: str, nth: int = 0) -> dict[str, Any]:
        return self._request("POST", "/api/tap_text", {"text": text, "nth": nth})

    def tap_element(self, element_id: str) -> dict[str, Any]:
        return self._request("POST", "/api/tap_element", {"id": element_id})

    def type_text(self, text: str) -> dict[str, Any]:
        return self._request("POST", "/api/type", {"text": text})

    def key(self, combo: str) -> dict[str, Any]:
        return self._request("POST", "/api/key", {"combo": combo})

    def scroll(self, dy: int, dx: int = 0) -> dict[str, Any]:
        return self._request("POST", "/api/scroll", {"dy": dy, "dx": dx})

    def long_press(self, x: float, y: float, ms: int = 800) -> dict[str, Any]:
        return self._request("POST", "/api/long_press", {"x": x, "y": y, "ms": ms})

    def drag(self, x1: float, y1: float, x2: float, y2: float, steps: int = 10) -> dict[str, Any]:
        return self._request("POST", "/api/drag", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "steps": steps})
