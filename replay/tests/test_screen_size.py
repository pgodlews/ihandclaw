"""Replay keeps the recorded screen size (manifest screen_size_pt) everywhere."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from replay.builder import DatasetBuilder
from replay.graph import ScreenGraph, ScreenNode, Transition
from replay.manifest import SessionManifest
from replay.recorder import SessionRecorder
from replay.server import create_replay_app, public_size
from replay.tests.test_builder import create_mock_session

SIZE = [390, 844]  # Synthetic: frames are generated here at @3x, not recorded from a phone.
HEADERS = {"Authorization": "Bearer t"}


def frame(color=(240, 240, 245)):
    return Image.new("RGB", (3 * SIZE[0], 3 * SIZE[1]), color)


def test_recorder_takes_the_size_from_daemon_state(tmp_path: Path):
    buf = BytesIO()
    frame().save(buf, "JPEG")

    async def state(request):
        return JSONResponse({"screen": {"pt": SIZE}})

    async def jpeg(request):
        return Response(buf.getvalue(), media_type="image/jpeg")

    async def elements(request):
        return JSONResponse({"elements": []})

    async def action(request):
        return JSONResponse({"status": "ok"})

    app = Starlette(routes=[Route("/api/state", state), Route("/api/frame.jpg", jpeg),
                            Route("/api/elements", elements),
                            Route("/api/{action}", action, methods=["POST"])])
    recorder = SessionRecorder("http://mock", "t", tmp_path / "s")
    recorder.client = TestClient(app, base_url="http://mock")
    assert SessionManifest.load(tmp_path / "s/manifest.json").screen_size_pt == [375, 812]
    recorder.record_step("tap", x=380, y=830)
    assert SessionManifest.load(tmp_path / "s/manifest.json").screen_size_pt == SIZE
    fixed = SessionRecorder("http://mock", "t", tmp_path / "f", screen_size_pt=[430, 932])
    fixed.client = TestClient(app, base_url="http://mock")
    fixed.record_step("home")
    assert fixed.manifest.screen_size_pt == [430, 932]


def _session(path: Path, size):
    create_mock_session(path, path.name, [{"pre_img": frame(), "post_img": frame((20, 20, 20)),
                                           "action": "home"}])
    manifest = SessionManifest.load(path / "manifest.json")
    manifest.screen_size_pt = size
    manifest.save(path / "manifest.json")


def test_builder_carries_the_size_and_refuses_mixed_phones(tmp_path: Path):
    _session(tmp_path / "a", SIZE)
    _session(tmp_path / "b", SIZE)
    graph = DatasetBuilder().merge_sessions([tmp_path / "a", tmp_path / "b"], output_dir=tmp_path / "g")
    assert graph.screen_size_pt == SIZE
    assert ScreenGraph.load(tmp_path / "g/graph.json").screen_size_pt == SIZE
    _session(tmp_path / "xs", [375, 812])
    with pytest.raises(ValueError, match="separate graphs"):
        DatasetBuilder().merge_sessions([tmp_path / "a", tmp_path / "xs"])
    assert ScreenGraph.from_dict({"nodes": {}}).screen_size_pt == [375, 812]


def test_server_serves_the_recorded_size(tmp_path: Path):
    graph = ScreenGraph(screen_size_pt=SIZE)
    frame().save(tmp_path / "home.jpg")
    graph.add_node(ScreenNode(screen_id="home", frame_path=str(tmp_path / "home.jpg"),
                              elements=[{"id": "t0", "text": "Edge", "bounds_pt": [360, 800, 390, 844],
                                         "center": [375, 822]}],
                              pointer={"x": 380.0, "y": 830.0, "confidence": 1.0}))
    graph.add_node(ScreenNode(screen_id="next", frame_path=str(tmp_path / "missing.jpg")))
    graph.add_transition(Transition(transition_id="t1", source_id="home", target_id="next", action_name="tap",
                                    action_args={"x": 375, "y": 822}))
    client = TestClient(create_replay_app(graph, token="t"))
    assert client.get("/api/state", headers=HEADERS).json()["screen"]["pt"] == SIZE
    pt2 = Image.open(BytesIO(client.get("/api/frame.jpg?scale=pt2", headers=HEADERS).content))
    assert pt2.size == public_size(SIZE) == (780, 1688)
    annotated = Image.open(BytesIO(client.get("/api/elements?annotate=1", headers=HEADERS).content))
    assert annotated.size == (1170, 2532)
    moved = client.post("/api/tap", json={"x": 376, "y": 823}, headers=HEADERS).json()
    assert moved["state"]["current_screen_id"] == "next"
    fallback = Image.open(BytesIO(client.get("/api/frame.jpg", headers=HEADERS).content))
    assert fallback.size == (1170, 2532)  # missing frame: synthetic @3x of the graph's size
