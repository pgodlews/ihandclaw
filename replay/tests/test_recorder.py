"""Tests for replay/recorder.py client and proxy recording."""
from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
from typing import Any

from PIL import Image
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from replay.manifest import SessionManifest
from replay.recorder import SessionRecorder
from replay.tests.conftest import make_screen_image


@pytest.fixture
def mock_daemon_app():
    img = make_screen_image("Daemon Screen")
    buf = BytesIO()
    img.save(buf, "JPEG")
    frame_bytes = buf.getvalue()

    elements_data = {
        "elements": [
            {
                "id": "vc1",
                "text": "Settings",
                "type": "vc_item",
                "bounds": "[50,150][120,220]",
                "bounds_pt": [50.0, 150.0, 120.0, 220.0],
                "center": [85.0, 185.0],
            }
        ],
        "pointer": {"x": 85.0, "y": 185.0, "confidence": 0.95},
        "tap_offset_pt": [10.0, 12.0],
    }

    state_data = {
        "paused": False,
        "current_screen_id": "home",
        "link": {"connected": True},
        "ble": {"conn": True, "ready": True},
    }

    async def get_frame(req: Request):
        return Response(frame_bytes, media_type="image/jpeg")

    async def get_elements(req: Request):
        return JSONResponse(elements_data)

    async def get_state(req: Request):
        return JSONResponse(state_data)

    async def do_action(req: Request):
        action = req.path_params["action"]
        body = await req.json()
        return JSONResponse({"status": "ok", "action": action, "args": body})

    routes = [
        Route("/api/frame.jpg", get_frame, methods=["GET"]),
        Route("/api/elements", get_elements, methods=["GET"]),
        Route("/api/state", get_state, methods=["GET"]),
        Route("/api/{action}", do_action, methods=["POST"]),
    ]
    return Starlette(routes=routes)


def test_session_recorder_direct_step(mock_daemon_app, tmp_path: Path):
    out_dir = tmp_path / "recording_session_1"
    recorder = SessionRecorder(
        daemon_url="http://mock-daemon",
        token="test-token",
        output_dir=out_dir,
        session_id="sess_001",
    )

    # Inject mock TestClient
    mock_client = TestClient(mock_daemon_app, base_url="http://mock-daemon")
    recorder.client = mock_client

    # Record a step
    result = recorder.record_step("tap", x=85.0, y=185.0)
    assert result["status"] == "ok"
    assert result["action"] == "tap"

    # Check manifest
    manifest_file = out_dir / "manifest.json"
    assert manifest_file.exists()
    manifest = SessionManifest.load(manifest_file)
    assert manifest.session_id == "sess_001"
    assert len(manifest.steps) == 1

    step0 = manifest.steps[0]
    assert step0.step == 0
    assert step0.action.name == "tap"
    assert step0.action.args == {"x": 85.0, "y": 185.0}

    # Verify frame and elements files exist and are valid
    pre_frame_file = out_dir / step0.pre_frame
    post_frame_file = out_dir / step0.post_frame
    elem_file = out_dir / step0.elements_file

    assert pre_frame_file.exists()
    assert post_frame_file.exists()
    assert elem_file.exists()

    pre_img = Image.open(pre_frame_file)
    assert pre_img.size == (1126, 2434)

    elem_json = json.loads(elem_file.read_text())
    assert elem_json["elements"][0]["id"] == "vc1"


def test_recording_proxy_mode(mock_daemon_app, tmp_path: Path):
    out_dir = tmp_path / "proxy_session_1"
    recorder = SessionRecorder(
        daemon_url="http://mock-daemon",
        token="test-token",
        output_dir=out_dir,
        session_id="proxy_sess_001",
    )
    mock_client = TestClient(mock_daemon_app, base_url="http://mock-daemon")
    recorder.client = mock_client

    # Test recorder.fetch_frame and fetch_elements
    frame_bytes = recorder.fetch_frame()
    assert len(frame_bytes) > 0
    elements = recorder.fetch_elements()
    assert "elements" in elements

    # Test record step with key action
    res = recorder.record_step("key", combo="cmd+space")
    assert res["status"] == "ok"
    assert res["args"] == {"combo": "cmd+space"}

    manifest = SessionManifest.load(out_dir / "manifest.json")
    assert len(manifest.steps) == 1
    assert manifest.steps[0].action.name == "key"
    assert manifest.steps[0].action.args == {"combo": "cmd+space"}
