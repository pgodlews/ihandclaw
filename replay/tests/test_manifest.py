"""Tests for manifest data structures and DroidClaw UIElement compatibility."""
from __future__ import annotations

import json
from pathlib import Path

from replay.manifest import (
    ActionRecord,
    SessionManifest,
    StepRecord,
    UIElement,
    is_point_in_bounds,
    parse_bounds,
)


def test_parse_bounds():
    # String format "[left,top][right,bottom]"
    assert parse_bounds("[10,20][50,60]") == [10.0, 20.0, 50.0, 60.0]
    # String format with floats
    assert parse_bounds("[10.5, 20.2][50.8, 60.1]") == [10.5, 20.2, 50.8, 60.1]
    # List format
    assert parse_bounds([15.0, 25.0, 35.0, 45.0]) == [15.0, 25.0, 35.0, 45.0]


def test_is_point_in_bounds():
    bounds = [10.0, 20.0, 50.0, 60.0]
    # Inside point
    assert is_point_in_bounds((30.0, 40.0), bounds)
    # Exact edge points
    assert is_point_in_bounds((10.0, 20.0), bounds)
    assert is_point_in_bounds((50.0, 60.0), bounds)
    # Outside point
    assert not is_point_in_bounds((55.0, 40.0), bounds)
    # Outside point within tolerance
    assert is_point_in_bounds((55.0, 40.0), bounds, tolerance=10.0)


def test_ui_element_initialization():
    elem = UIElement(
        id="vc1",
        text="Settings",
        type="vc_item",
        bounds="[20,30][80,90]",
        center=[],
        size=[],
    )
    assert elem.bounds_pt == [20.0, 30.0, 80.0, 90.0]
    assert elem.center == [50.0, 60.0]
    assert elem.size == [60.0, 60.0]
    assert elem.clickable is True

    # Check to_dict and from_dict
    d = elem.to_dict()
    reconstructed = UIElement.from_dict(d)
    assert reconstructed.id == "vc1"
    assert reconstructed.center == [50.0, 60.0]


def test_manifest_save_and_load(tmp_path: Path):
    manifest = SessionManifest(
        session_id="test_session_1",
        created_at="2026-10-10T10:00:00Z",
        daemon_url="http://iphone-hand.local:8790",
        steps=[
            StepRecord(
                step=0,
                timestamp=100.0,
                action=ActionRecord(name="tap", args={"x": 50.0, "y": 60.0}),
                pre_frame="frames/frame_0000_pre.jpg",
                elements_file="elements/elements_0000.json",
                post_frame="frames/frame_0000_post.jpg",
                response={"result": {"status": "ok"}, "state": {"paused": False}},
            )
        ],
    )

    path = tmp_path / "manifest.json"
    manifest.save(path)
    assert path.exists()

    loaded = SessionManifest.load(path)
    assert loaded.session_id == "test_session_1"
    assert len(loaded.steps) == 1
    step = loaded.steps[0]
    assert step.step == 0
    assert step.action.name == "tap"
    assert step.action.args == {"x": 50.0, "y": 60.0}
    assert step.response["result"]["status"] == "ok"
