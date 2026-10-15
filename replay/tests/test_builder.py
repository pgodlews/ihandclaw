"""Tests for replay/builder.py merging multiple sessions via pHash and exporting summaries."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PIL import Image
import pytest

from replay.builder import DatasetBuilder
from replay.manifest import ActionRecord, SessionManifest, StepRecord
from replay.tests.conftest import make_screen_image


def create_mock_session(
    session_dir: Path,
    session_id: str,
    steps_data: list[dict[str, Any]],
):
    """Create a full mock recorded session folder on disk."""
    frames_dir = session_dir / "frames"
    elements_dir = session_dir / "elements"
    frames_dir.mkdir(parents=True, exist_ok=True)
    elements_dir.mkdir(parents=True, exist_ok=True)

    manifest_steps: list[StepRecord] = []

    for i, s in enumerate(steps_data):
        pre_path = frames_dir / f"frame_{i:04d}_pre.jpg"
        post_path = frames_dir / f"frame_{i:04d}_post.jpg"
        elem_path = elements_dir / f"elements_{i:04d}.json"

        s["pre_img"].save(pre_path, "JPEG")
        s["post_img"].save(post_path, "JPEG")
        elem_path.write_text(json.dumps(s.get("elements", {"elements": []})))

        manifest_steps.append(
            StepRecord(
                step=i,
                timestamp=100.0 + i,
                action=ActionRecord(name=s["action"], args=s.get("args", {})),
                pre_frame=f"frames/frame_{i:04d}_pre.jpg",
                elements_file=f"elements/elements_{i:04d}.json",
                post_frame=f"frames/frame_{i:04d}_post.jpg",
                response=s.get("response", {"status": "ok"}),
            )
        )

    manifest = SessionManifest(
        session_id=session_id,
        created_at="2026-10-10T10:00:00Z",
        daemon_url="http://iphone-hand.local:8790",
        steps=manifest_steps,
    )
    manifest.save(session_dir / "manifest.json")


def test_merge_sessions_merges_identical_screens(tmp_path: Path):
    sess1_dir = tmp_path / "session_1"
    sess2_dir = tmp_path / "session_2"
    merged_dir = tmp_path / "merged"

    # Images for Session 1: Home (10:00) -> Settings -> Dark Mode
    home_img_1 = make_screen_image(
        "Home Screen",
        clock_time="10:00",
        items=[("Settings", (50, 150, 120, 220)), ("Safari", (150, 150, 220, 220))],
    )
    settings_img = make_screen_image(
        "Settings Screen",
        bg_color=(235, 235, 240),
        items=[("Wi-Fi", (20, 120, 355, 170)), ("Bluetooth", (20, 180, 355, 230))],
    )
    dark_mode_img = make_screen_image(
        "Dark Mode Screen",
        bg_color=(20, 20, 25),
        items=[("Appearance Dark", (20, 150, 355, 200))],
    )

    create_mock_session(
        sess1_dir,
        "sess_1",
        [
            {
                "pre_img": home_img_1,
                "post_img": settings_img,
                "action": "tap",
                "args": {"x": 85.0, "y": 185.0},
                "elements": {
                    "elements": [
                        {
                            "id": "vc1",
                            "text": "Settings",
                            "bounds_pt": [50.0, 150.0, 120.0, 220.0],
                            "bounds": "[50,150][120,220]",
                        }
                    ]
                },
            },
            {
                "pre_img": settings_img,
                "post_img": dark_mode_img,
                "action": "tap",
                "args": {"x": 100.0, "y": 200.0},
                "elements": {
                    "elements": [
                        {
                            "id": "vc1",
                            "text": "Appearance",
                            "bounds_pt": [50.0, 180.0, 300.0, 220.0],
                            "bounds": "[50,180][300,220]",
                        }
                    ]
                },
            },
        ],
    )

    # Images for Session 2: Home (10:45 - clock changed!) -> Safari
    home_img_2 = make_screen_image(
        "Home Screen",
        clock_time="10:45",
        items=[("Settings", (50, 150, 120, 220)), ("Safari", (150, 150, 220, 220))],
    )
    safari_img = make_screen_image(
        "Safari Screen",
        bg_color=(255, 255, 255),
        items=[("Search", (30, 700, 345, 750))],
    )

    create_mock_session(
        sess2_dir,
        "sess_2",
        [
            {
                "pre_img": home_img_2,
                "post_img": safari_img,
                "action": "tap",
                "args": {"x": 185.0, "y": 185.0},
                "elements": {
                    "elements": [
                        {
                            "id": "vc2",
                            "text": "Safari",
                            "bounds_pt": [150.0, 150.0, 220.0, 220.0],
                            "bounds": "[150,150][220,220]",
                        }
                    ]
                },
            }
        ],
    )

    # Merge sessions using perceptual hash (threshold 6)
    builder = DatasetBuilder(hash_threshold=6, hash_method="phash")
    graph = builder.merge_sessions([sess1_dir, sess2_dir], output_dir=merged_dir)

    # Home Screen from Sess 1 and Home Screen from Sess 2 MUST BE MERGED!
    # Expected unique screens: Home, Settings, Dark Mode, Safari = 4 total screens (not 5!)
    assert len(graph.nodes) == 4

    # The canonical Home node must have 2 outgoing transitions (branching!)
    initial_node = graph.nodes[graph.initial_screen_id]
    assert len(initial_node.outgoing_transitions) == 2

    actions_from_home = {t.action_args.get("x") for t in initial_node.outgoing_transitions}
    assert 85.0 in actions_from_home  # Settings tap
    assert 185.0 in actions_from_home  # Safari tap

    # Check generated files in merged_dir
    assert (merged_dir / "graph.json").exists()
    assert (merged_dir / "summary.json").exists()
    assert (merged_dir / "summary.md").exists()


def test_builder_summary_export(tmp_path: Path):
    sess_dir = tmp_path / "single_sess"
    merged_dir = tmp_path / "single_merged"

    home = make_screen_image("Home", items=[("App1", (20, 20, 100, 100))])
    app1 = make_screen_image("App1 Screen", items=[("Details", (50, 400, 300, 500))])

    create_mock_session(
        sess_dir,
        "s1",
        [{"pre_img": home, "post_img": app1, "action": "launch", "args": {"app": "App1"}}],
    )

    builder = DatasetBuilder(hash_method="phash")
    graph = builder.merge_sessions([sess_dir], output_dir=merged_dir)

    summary_dict = builder.generate_summary(graph)
    assert summary_dict["total_screens"] == 2
    assert summary_dict["total_transitions"] == 1
    assert summary_dict["action_counts"] == {"launch": 1}

    # Markdown export
    md = builder.export_summary(graph, format="markdown")
    assert "# iPhone Hand Replay Simulator - Dataset Summary" in md
    assert "Total Unique Screens" in md
    assert "`launch`" in md

    # JSON export
    js = builder.export_summary(graph, format="json")
    parsed = json.loads(js)
    assert parsed["total_screens"] == 2

    # Graphviz DOT export
    dot = builder.export_summary(graph, format="dot")
    assert "digraph ScreenGraph" in dot
    assert "launch" in dot
    assert "->" in dot
