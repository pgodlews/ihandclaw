"""Tests for ScreenGraph, ScreenNode, Transitions, and action matching rules."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from replay.graph import ScreenGraph, ScreenNode, Transition, normalize_combo, normalize_text


def test_normalize_combo():
    assert normalize_combo("Shift+Cmd+Space") == "cmd+shift+space"
    assert normalize_combo("cmd+space") == "cmd+space"
    assert normalize_combo("Enter") == "enter"
    assert normalize_combo("CMD+H") == "cmd+h"


def test_screen_node_element_queries():
    node = ScreenNode(
        screen_id="s1",
        frame_path="frames/s1.jpg",
        elements=[
            {
                "id": "vc1",
                "text": "Settings",
                "type": "vc_item",
                "bounds_pt": [50.0, 100.0, 150.0, 200.0],
                "bounds": "[50,100][150,200]",
                "center": [100.0, 150.0],
            },
            {
                "id": "vc2",
                "text": "Safari Browser",
                "type": "vc_item",
                "bounds_pt": [200.0, 100.0, 300.0, 200.0],
                "bounds": "[200,100][300,200]",
                "center": [250.0, 150.0],
            },
        ],
    )

    # find_element_at
    elem = node.find_element_at(100.0, 150.0)
    assert elem is not None
    assert elem["id"] == "vc1"

    elem_outside = node.find_element_at(10.0, 10.0)
    assert elem_outside is None

    # find_element_by_id
    assert node.find_element_by_id("vc2")["text"] == "Safari Browser"
    assert node.find_element_by_id("vc99") is None

    # find_element_by_text
    assert node.find_element_by_text("Settings")["id"] == "vc1"
    assert node.find_element_by_text("safari")["id"] == "vc2"


def test_screen_graph_tap_matching():
    graph = ScreenGraph()
    node1 = ScreenNode(
        screen_id="s1",
        frame_path="frames/s1.jpg",
        elements=[
            {
                "id": "vc1",
                "text": "Settings",
                "bounds_pt": [50.0, 100.0, 150.0, 200.0],
                "bounds": "[50,100][150,200]",
                "center": [100.0, 150.0],
            }
        ],
    )
    node2 = ScreenNode(screen_id="s2", frame_path="frames/s2.jpg")
    graph.add_node(node1)
    graph.add_node(node2)

    # Transition tapping Settings button
    t1 = Transition(
        transition_id="t1",
        source_id="s1",
        target_id="s2",
        action_name="tap",
        action_args={"x": 100.0, "y": 150.0},
        target_element=node1.elements[0],
    )
    graph.add_transition(t1)

    # 1. Tap within element bbox (e.g. 70, 120) should match
    match = graph.match_action("s1", "tap", {"x": 70.0, "y": 120.0})
    assert match is not None
    target, trans = match
    assert target.screen_id == "s2"
    assert trans.transition_id == "t1"

    # 2. Tap close to recorded point within tolerance (e.g. 108, 155 -> dist ~9.4 <= 15)
    match_near = graph.match_action("s1", "tap", {"x": 108.0, "y": 155.0})
    assert match_near is not None

    # 3. Off-path tap far away (e.g. 300, 500) must return None
    match_far = graph.match_action("s1", "tap", {"x": 300.0, "y": 500.0})
    assert match_far is None


def test_screen_graph_other_actions_matching():
    graph = ScreenGraph()
    s_home = ScreenNode(screen_id="home", frame_path="home.jpg")
    s_spotlight = ScreenNode(screen_id="spotlight", frame_path="spot.jpg")
    s_safari = ScreenNode(screen_id="safari", frame_path="safari.jpg")
    s_settings = ScreenNode(screen_id="settings", frame_path="settings.jpg")

    graph.add_node(s_home)
    graph.add_node(s_spotlight)
    graph.add_node(s_safari)
    graph.add_node(s_settings)

    # Transitions from home
    graph.add_transition(Transition("t_key", "home", "spotlight", "key", {"combo": "cmd+space"}))
    graph.add_transition(Transition("t_launch", "home", "safari", "launch", {"app": "Safari"}))
    graph.add_transition(Transition("t_home", "settings", "home", "home", {}))

    # Test key matching
    match_key = graph.match_action("home", "key", {"combo": "Cmd+Space"})
    assert match_key is not None
    assert match_key[0].screen_id == "spotlight"

    # Test key mismatch -> off-path
    assert graph.match_action("home", "key", {"combo": "enter"}) is None

    # Test launch matching
    match_launch = graph.match_action("home", "launch", {"app": "safari"})
    assert match_launch is not None
    assert match_launch[0].screen_id == "safari"

    # Test launch mismatch
    assert graph.match_action("home", "launch", {"app": "Camera"}) is None

    # Test home action matching from settings
    match_home = graph.match_action("settings", "home", {})
    assert match_home is not None
    assert match_home[0].screen_id == "home"

    # Test cmd+h key matching home transition
    match_cmd_h = graph.match_action("settings", "key", {"combo": "cmd+h"})
    assert match_cmd_h is not None
    assert match_cmd_h[0].screen_id == "home"


def test_graph_serialization(tmp_path: Path):
    graph = ScreenGraph(initial_screen_id="node1")
    n1 = ScreenNode("node1", "f1.jpg", elements=[{"id": "t1", "text": "Hi"}])
    n2 = ScreenNode("node2", "f2.jpg")
    graph.add_node(n1)
    graph.add_node(n2)
    graph.add_transition(Transition("t1", "node1", "node2", "home", {}))

    save_path = tmp_path / "graph.json"
    graph.save(save_path)
    assert save_path.exists()

    loaded = ScreenGraph.load(save_path)
    assert loaded.initial_screen_id == "node1"
    assert len(loaded.nodes) == 2
    assert len(loaded.nodes["node1"].outgoing_transitions) == 1
