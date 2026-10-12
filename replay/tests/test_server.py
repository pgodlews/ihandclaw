"""Tests for replay/server.py HTTP server routes, token auth, and off-path logging."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from PIL import Image
import pytest

from replay.graph import ScreenGraph, ScreenNode, Transition
from replay.server import ReplayServer, create_replay_app
from replay.tests.conftest import make_screen_image


@pytest.fixture
def mock_graph(tmp_path: Path) -> tuple[ScreenGraph, Path]:
    graph = ScreenGraph()

    # Create synthetic frame images on disk
    f1 = tmp_path / "f1.jpg"
    f2 = tmp_path / "f2.jpg"
    f3 = tmp_path / "f3_password.jpg"

    img1 = make_screen_image("Home", items=[("Settings", (50, 150, 120, 220))])
    img2 = make_screen_image("Settings")
    img3 = make_screen_image("Password Prompt")

    img1.save(f1, "JPEG")
    img2.save(f2, "JPEG")
    img3.save(f3, "JPEG")

    node1 = ScreenNode(
        screen_id="home",
        frame_path=str(f1),
        elements=[
            {
                "id": "vc1",
                "text": "Settings",
                "type": "vc_item",
                "bounds": "[50,150][120,220]",
                "bounds_pt": [50.0, 150.0, 120.0, 220.0],
                "center": [85.0, 185.0],
                "size": [70.0, 70.0],
                "clickable": True,
            }
        ],
        pointer={"x": 100.0, "y": 200.0, "confidence": 0.9},
    )

    node2 = ScreenNode(
        screen_id="settings",
        frame_path=str(f2),
        elements=[
            {
                "id": "vc1",
                "text": "Dark Mode",
                "type": "vc_item",
                "bounds": "[20,120][355,170]",
                "bounds_pt": [20.0, 120.0, 355.0, 170.0],
                "center": [187.5, 145.0],
            }
        ],
    )

    node3 = ScreenNode(
        screen_id="password_screen",
        frame_path=str(f3),
        elements=[
            {
                "id": "t0",
                "text": "Enter Passcode",
                "type": "text",
                "bounds": "[50,200][325,250]",
                "bounds_pt": [50.0, 200.0, 325.0, 250.0],
            }
        ],
    )

    graph.add_node(node1)
    graph.add_node(node2)
    graph.add_node(node3)

    # Transitions
    graph.add_transition(
        Transition(
            transition_id="t_tap_settings",
            source_id="home",
            target_id="settings",
            action_name="tap",
            action_args={"x": 85.0, "y": 185.0},
            target_element=node1.elements[0],
            response_result={"status": "opened_settings"},
        )
    )
    graph.add_transition(
        Transition(
            transition_id="t_home",
            source_id="settings",
            target_id="home",
            action_name="home",
            action_args={},
            response_result={"status": "returned_home"},
        )
    )
    graph.add_transition(
        Transition(
            transition_id="t_to_pw",
            source_id="home",
            target_id="password_screen",
            action_name="key",
            action_args={"combo": "cmd+l"},
        )
    )

    return graph, tmp_path


@pytest.fixture
def test_client(mock_graph):
    graph, base_dir = mock_graph
    app = create_replay_app(graph=graph, token="test-secret-token", tolerance_pt=15.0, base_dir=base_dir)
    from starlette.testclient import TestClient
    return TestClient(app, base_url="http://test")


def test_auth_enforcement(test_client):
    # 1. No token -> 401
    resp = test_client.get("/api/state")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"

    # 2. Wrong token -> 401
    resp_wrong = test_client.get("/api/state", headers={"Authorization": "Bearer wrong-token"})
    assert resp_wrong.status_code == 401

    # 3. Valid Bearer token -> 200
    resp_valid = test_client.get("/api/state", headers={"Authorization": "Bearer test-secret-token"})
    assert resp_valid.status_code == 200
    assert resp_valid.json()["current_screen_id"] == "home"

    # 4. Valid query param token -> 200
    resp_query = test_client.get("/api/frame.jpg?token=test-secret-token")
    assert resp_query.status_code == 200
    assert resp_query.headers["content-type"] == "image/jpeg"

    # 5. Cross-origin request refused -> 403
    resp_cors = test_client.get(
        "/api/state",
        headers={"Authorization": "Bearer test-secret-token", "Origin": "http://malicious.com"},
    )
    assert resp_cors.status_code == 403


def test_frame_and_scale(test_client):
    headers = {"Authorization": "Bearer test-secret-token"}

    # Default scale
    resp = test_client.get("/api/frame.jpg", headers=headers)
    assert resp.status_code == 200
    img = Image.open(BytesIO(resp.content))
    assert img.size == (1126, 2434)

    # Scale pt2 -> 750x1624 (2 px per pt for 375x812 pt)
    resp_pt2 = test_client.get("/api/frame.jpg?scale=pt2", headers=headers)
    assert resp_pt2.status_code == 200
    img_pt2 = Image.open(BytesIO(resp_pt2.content))
    assert img_pt2.size == (750, 1624)

    # Invalid scale -> 422
    resp_bad = test_client.get("/api/frame.jpg?scale=invalid", headers=headers)
    assert resp_bad.status_code == 422


def test_elements_and_annotate(test_client):
    headers = {"Authorization": "Bearer test-secret-token"}

    # JSON elements
    resp = test_client.get("/api/elements", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "elements" in data
    assert len(data["elements"]) == 1
    elem = data["elements"][0]
    assert elem["id"] == "vc1"
    assert elem["text"] == "Settings"
    assert "center" in elem
    assert "bounds" in elem
    assert data["pointer"] == {"x": 100.0, "y": 200.0, "confidence": 0.9}

    # Annotate query param -> image with overlays
    resp_ann = test_client.get("/api/elements?annotate=1", headers=headers)
    assert resp_ann.status_code == 200
    assert resp_ann.headers["content-type"] == "image/jpeg"
    ann_img = Image.open(BytesIO(resp_ann.content))
    assert ann_img.size == (1126, 2434)


def test_web_ui_dashboard(test_client):
    resp = test_client.get("/", headers={"Authorization": "Bearer test-secret-token"})
    assert resp.status_code == 200
    assert "iPhone Hand Replay Simulator" in resp.text
    assert "home" in resp.text


def test_pause_and_resume(test_client):
    headers = {"Authorization": "Bearer test-secret-token"}

    # Pause
    resp = test_client.post("/api/pause", json={"paused": True}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["paused"] is True

    # Action while paused -> 423
    resp_act = test_client.post("/api/tap", json={"x": 85.0, "y": 185.0}, headers=headers)
    assert resp_act.status_code == 423

    # Unpause
    resp_unpause = test_client.post("/api/pause", json={"paused": False}, headers=headers)
    assert resp_unpause.status_code == 200
    assert resp_unpause.json()["paused"] is False


def test_action_matching_and_off_path(test_client):
    headers = {"Authorization": "Bearer test-secret-token"}

    # Initial screen is home
    state = test_client.get("/api/state", headers=headers).json()
    assert state["current_screen_id"] == "home"
    assert state["off_path_count"] == 0

    # 1. Matching tap (inside Settings bbox: [50, 150, 120, 220])
    tap_resp = test_client.post("/api/tap", json={"x": 80.0, "y": 190.0}, headers=headers)
    assert tap_resp.status_code == 200
    res = tap_resp.json()
    assert res["result"]["status"] == "opened_settings"
    assert res["state"]["current_screen_id"] == "settings"
    assert res["state"]["matched_actions"] == 1
    assert res["state"]["off_path_count"] == 0

    # Current screen is now settings.
    # 2. Off-path action: tap random area where no transition is recorded
    off_resp = test_client.post("/api/tap", json={"x": 10.0, "y": 10.0}, headers=headers)
    assert off_resp.status_code == 200
    off_data = off_resp.json()
    assert off_data["result"]["status"] == "off-path"
    assert off_data["result"]["matched"] is False
    # Remains on settings screen!
    assert off_data["state"]["current_screen_id"] == "settings"
    assert off_data["state"]["off_path_count"] == 1

    # 3. Matching home action
    home_resp = test_client.post("/api/home", json={}, headers=headers)
    assert home_resp.status_code == 200
    assert home_resp.json()["state"]["current_screen_id"] == "home"

    # 4. Reset action
    reset_resp = test_client.post("/api/reset", headers=headers)
    assert reset_resp.status_code == 200
    assert reset_resp.json()["current_screen_id"] == "home"


def test_password_prompt_safety_guard(test_client):
    headers = {"Authorization": "Bearer test-secret-token"}

    # Move to password prompt screen
    key_resp = test_client.post("/api/key", json={"combo": "cmd+l"}, headers=headers)
    assert key_resp.status_code == 200
    assert key_resp.json()["state"]["current_screen_id"] == "password_screen"

    # Attempting to type on password screen must be refused with 409
    type_resp = test_client.post("/api/type", json={"text": "secret123"}, headers=headers)
    assert type_resp.status_code == 409
    assert "password" in type_resp.json()["error"].lower()
