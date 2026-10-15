"""All captions, applications and images below are synthetic; no device I/O."""

import asyncio
from dataclasses import asdict
import inspect
from io import BytesIO
import socket
from types import SimpleNamespace

from PIL import Image
import pytest

from iphone_hand.devservices import DevServices, DevServicesError, OCRBox, match_to_ocr, parse_caption
from iphone_hand.devservices._pmd import PmdBackend, load_api


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("developer-services tests must not open a network connection")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.parametrize("caption,label,roles,value,states,hints", [
    ("Example Radio, 0, Button, Toggle", "Example Radio", ("Button", "Toggle"), "0", (), ()),
    ("Example Connection, On, Button", "Example Connection", ("Button",), None, ("On",), ()),
    ("Example Panel, Header", "Example Panel", ("Header",), None, (), ()),
    ("Demo Call, Updates Frequently, Double-tap to open, Button", "Demo Call", ("Button",),
     None, (), ("Updates Frequently", "Double-tap to open")),
    ("Find Example, Search Field, Enter a phrase", "Find Example", ("Search Field",), "Enter a phrase", (), ()),
    ("Example Feed, Tab, Selected", "Example Feed", ("Tab",), None, ("Selected",), ()),
    ("Example Site, Link, Image", "Example Site", ("Link", "Image"), None, (), ()),
    ("Example Input, Text Field, sample value", "Example Input", ("Text Field",), "sample value", (), ()),
    ("Example Level, 42%, Adjustable, Swipe up to adjust", "Example Level", ("Adjustable",), "42%", (),
     ("Swipe up to adjust",)),
    ("Example Action, Button, Dimmed, Not Selected", "Example Action", ("Button",), None,
     ("Dimmed", "Not Selected"), ()),
    ("  Example  , button, BUTTON, off,  ", "Example", ("Button",), None, ("Off",), ()),
    ("Example, Unknown Trait, odd value", "Example", (), "Unknown Trait, odd value", (), ()),
    (None, "", (), None, (), ()),
])
def test_caption_semantics(caption, label, roles, value, states, hints):
    element = parse_caption(caption)
    assert (element.label, element.roles, element.value, element.states, element.hints) == (
        label, roles, value, states, hints)


def test_caption_private_fields_not_in_repr():
    item = parse_caption("Synthetic Private Label, synthetic value, Button, Double-tap to open",
                         uid="synthetic-id", spoken_description="synthetic speech")
    assert "Synthetic" not in repr(item)
    assert "synthetic" not in repr(item)
    assert asdict(item)["caption"].startswith("Synthetic")


def test_ocr_fuzzy_truncation_and_same_row_merge():
    items = [parse_caption(c) for c in ("Demo Connection, On, Button", "Example Preferences, Header",
                                       "Demo Library, Button", "Missing Example, Button")]
    boxes = [OCRBox("Demo", (10, 10, 45, 25)), OCRBox("Connectlon", (48, 10, 115, 25)),
             OCRBox("Example Pref…", (10, 50, 140, 65)), OCRBox("Demo Library", (10, 90, 100, 105))]
    matches = match_to_ocr(items, boxes)
    assert matches[0].bounds == (10, 10, 115, 25)
    assert matches[0].ocr_indices == (0, 1)
    assert matches[0].confidence > 0.8
    assert matches[1].bounds == boxes[2].bounds
    assert 0.8 < matches[1].confidence < 1
    assert matches[2].center == (55, 97.5)
    assert matches[3].bounds is None and matches[3].confidence == 0


def test_ocr_prefers_state_evidence_on_same_row():
    item = parse_caption("Example, On, Button")
    boxes = [OCRBox("Example", (10, 10, 90, 25)), OCRBox("Off", (150, 10, 180, 25)),
             OCRBox("Example", (10, 50, 90, 65)), OCRBox("On", (150, 50, 180, 65))]
    match = match_to_ocr([item], boxes)[0]
    assert match.bounds == boxes[2].bounds and match.confidence == 1


def test_ocr_duplicate_labels_ambiguous_and_one_to_one():
    items = [parse_caption("Example, Button", uid="first"), parse_caption("Example, Button", uid="second")]
    boxes = [OCRBox("Example", (10, 10, 80, 25)), OCRBox("Example", (10, 100, 80, 115))]
    matches = match_to_ocr(items, boxes)
    assert len({m.ocr_indices for m in matches}) == 2
    assert all(m.confidence <= 0.6 for m in matches)
    hinted = match_to_ocr([items[0]], boxes, row_hints={"first": 107.5})[0]
    assert hinted.bounds == boxes[1].bounds and hinted.confidence == 1
    one_box = match_to_ocr(items, boxes[:1])
    assert sum(m.bounds is not None for m in one_box) == 1


def test_ocr_does_not_merge_rows_or_unrelated_short_text():
    boxes = [OCRBox("Demo", (10, 10, 50, 25)), OCRBox("Library", (10, 50, 100, 65))]
    assert match_to_ocr([parse_caption("Demo Library, Button")], boxes)[0].bounds is None
    assert match_to_ocr([parse_caption("Window, Button")], [OCRBox("Wi", (0, 0, 10, 10))])[0].bounds is None
    with pytest.raises(ValueError):
        OCRBox("Example", (0, 0, -1, 10))


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.delay = 0
        self.fail = False
        self.active = 0
        self.peak = 0

    async def connect(self, device_id):
        self.calls.append("connect")

    async def mount(self):
        self.calls.append("mount")

    async def close(self):
        self.calls.append("close")

    async def iter_items(self):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.fail:
                raise RuntimeError("Synthetic private label should never escape")
            for k in range(3):
                self.calls.append(f"item{k}")
                yield {"caption": f"Example {k}, Button", "estimated_uid": f"item{k}",
                       "platform_identifier": f"platform{k}", "spoken_description": "Synthetic speech"}
        finally:
            self.active -= 1

    async def apps(self):
        return [{"CFBundleIdentifier": "org.example.demo", "CFBundleDisplayName": "Example Demo"},
                {"CFBundleIdentifier": "org.example.other", "CFBundleName": "Other Example"},
                {"CFBundleIdentifier": "org.example.fallback"}]

    async def launch(self, bundle):
        self.calls.append(("launch", bundle))
        return 123

    async def terminate(self, bundle):
        return bundle == "org.example.demo"

    async def kill(self, pid):
        self.calls.append(("kill", pid))

    async def screenshot(self):
        out = BytesIO()
        Image.new("RGB", (30, 60)).save(out, format="PNG")
        return out.getvalue()

    async def show_visuals(self, enabled):
        self.calls.append(("visuals", enabled))

    async def supported_audit_types(self):
        return [100]

    async def audit_issues(self, types):
        return [{"element_rect_value": "{{1, 2}, {3, 4}}"}]


def test_bridge_operations_and_focus_stop_at_target():
    async def exercise():
        backend = FakeBackend()
        async with DevServices(backend=backend) as dev:
            items = await dev.list_items()
            assert len(items) == 3 and items[0].uid == "item0"
            assert await dev.resolve_bundle("example demo") == "org.example.demo"
            assert await dev.resolve_bundle("org.example.unknown") == "org.example.unknown"
            assert await dev.launch("org.example.demo") == 123
            assert await dev.terminate("org.example.demo")
            assert not await dev.terminate("org.example.other")
            await dev.kill(123)
            shot = await dev.screenshot()
            assert (shot.width, shot.height, shot.format, shot.evidence) == (30, 60, "PNG", "measured")
            assert shot.elapsed_ms >= 0
            assert await dev.supported_audit_types() == [100]
            assert (await dev.audit_issues([100]))[0]["element_rect_value"] == "{{1, 2}, {3, 4}}"
            backend.calls.clear()
            assert (await dev.focus("item1")).label == "Example 1"
            assert backend.calls == [("visuals", True), "item0", "item1"]
            assert backend.active == 0
            await dev.show_visuals(False)
        assert backend.calls[-1] == "close"
    asyncio.run(exercise())


def test_bridge_errors_timeouts_limits_and_serialization():
    async def exercise():
        backend = FakeBackend()
        dev = DevServices(backend=backend, timeout=0.02)
        await dev.connect()
        backend.delay = 0.001
        await asyncio.gather(dev.list_items(), dev.list_items())
        assert backend.peak == 1
        backend.fail = True
        with pytest.raises(DevServicesError) as error:
            await dev.list_items()
        assert "private" not in str(error.value)
        assert backend.calls[-1] == "close"
        with pytest.raises(DevServicesError, match="connect before"):
            await dev.list_items()
        backend.fail = False
        await dev.connect()
        backend.delay = 1
        with pytest.raises(DevServicesError, match="timed out"):
            await dev.list_items()
        assert backend.active == 0
        await dev.connect()
        task = asyncio.create_task(dev.list_items())
        await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend.active == 0 and backend.calls[-1] == "close"
        backend.delay = 0
        async with DevServices(backend=backend, auto_mount=False, max_items=1) as limited:
            with pytest.raises(DevServicesError, match="limit"):
                await limited.list_items()
        await dev.close()
    asyncio.run(exercise())


def test_name_ambiguity_and_validation():
    async def exercise():
        backend = FakeBackend()
        async def ambiguous():
            return [{"CFBundleIdentifier": "org.example.a", "CFBundleName": "Same Example"},
                    {"CFBundleIdentifier": "org.example.b", "CFBundleName": "Same Example"}]
        backend.apps = ambiguous
        async with DevServices(backend=backend) as dev:
            with pytest.raises(DevServicesError, match="ambiguous"):
                await dev.resolve_bundle("Same Example")
            with pytest.raises(ValueError):
                await dev.kill(True)
            with pytest.raises(ValueError):
                await dev.launch("not a bundle")
    asyncio.run(exercise())


def test_cli_dev_routes_without_pixel_connection(monkeypatch, capsys):
    import iphone_hand.cli as cli
    def forbidden(*args):
        pytest.fail("pixel connection created")
    async def run(args):
        return {"command": args.dev_command, "app": getattr(args, "app", None)}
    monkeypatch.setattr(cli, "make_phone", forbidden)
    monkeypatch.setattr(cli, "run_dev", run)
    for command in (["items"], ["apps"], ["mount"], ["launch", "Example Demo"]):
        assert cli.main(["dev", *command]) == 0
        assert '"command"' in capsys.readouterr().out
    assert cli.main(["--fake", "dev", "items"]) == 1


def test_cli_dev_real_dispatch_on_fake_backend(monkeypatch):
    from iphone_hand.cli import main
    import iphone_hand.devservices as module
    calls = []
    def factory(**kwargs):
        backend = FakeBackend()
        calls.append(backend.calls)
        return DevServices(backend=backend, **kwargs)
    monkeypatch.setattr(module, "DevServices", factory)
    assert main(["dev", "launch", "Example Demo"]) == 0
    assert calls[0] == ["connect", "mount", ("launch", "org.example.demo"), "close"]
    assert main(["dev", "apps"]) == 0
    assert "mount" not in calls[1]


def test_pmd_adapter_calls_and_closes_python_services():
    async def exercise():
        events = []
        class Service:
            def __init__(self, *args, **kwargs):
                pass
            async def connect(self):
                events.append("connect")
            async def __aenter__(self):
                events.append("enter")
                return self
            async def __aexit__(self, *exc):
                events.append("exit")
            async def close(self):
                events.append("close")
            async def browse(self, **kwargs):
                assert kwargs["options"] == {"ApplicationType": "Any"}
                assert kwargs["attributes"] == ["CFBundleIdentifier", "CFBundleDisplayName", "CFBundleName"]
                return []
            async def launch(self, bundle, *, kill_existing):
                assert bundle == "org.example.demo" and kill_existing is False
                return 123
            async def process_identifier_for_bundle_identifier(self, bundle):
                return 123
            async def kill(self, pid):
                events.append(("kill", pid))
            async def get_screenshot(self):
                return b"synthetic-image"
            async def set_show_visuals(self, enabled):
                events.append(("visuals", enabled))
        class AlreadyMountedError(Exception):
            pass
        async def tunnels():
            return {"synthetic-device": [{"tunnel-address": "synthetic-address",
                                          "tunnel-port": 1, "interface": "synthetic-interface"}]}
        async def mount(provider):
            raise AlreadyMountedError()
        api = SimpleNamespace(get_tunneld_tunnels=tunnels, RemoteServiceDiscoveryService=Service,
                              auto_mount=mount, AlreadyMountedError=AlreadyMountedError,
                              DvtProvider=Service, ProcessControl=Service, Screenshot=Service,
                              InstallationProxyService=Service, AccessibilityAudit=Service)
        backend = PmdBackend(api)
        await backend.connect(None)
        await backend.mount()
        assert await backend.apps() == []
        assert await backend.launch("org.example.demo") == 123
        assert await backend.terminate("org.example.demo")
        await backend.kill(456)
        assert await backend.screenshot() == b"synthetic-image"
        await backend.show_visuals(True)
        await backend.close()
        assert events.count("enter") == events.count("exit") + 1  # persistent audit
        assert events[-3:] == [("visuals", False), "close", "close"]
        async def multiple():
            return {"synthetic-a": [{}], "synthetic-b": [{}]}
        api.get_tunneld_tunnels = multiple
        with pytest.raises(RuntimeError, match="multiple devices"):
            await backend.connect(None)
    asyncio.run(exercise())


def test_pinned_api_imports_without_device_io(monkeypatch):
    from collections import namedtuple
    import platform
    # A transitive plumbum import probes `uname -p`. Keep the suite's blanket
    # subprocess prohibition intact and supply synthetic host metadata instead.
    metadata = namedtuple("HostMetadata", "system node release version machine processor")
    monkeypatch.setattr(platform, "uname", lambda: metadata("Darwin", "", "", "", "arm64", ""))
    pytest.importorskip("pymobiledevice3")
    api = load_api()
    assert inspect.iscoroutinefunction(api.auto_mount)
    assert inspect.iscoroutinefunction(api.ProcessControl.launch)
    assert inspect.iscoroutinefunction(api.Screenshot.get_screenshot)

    # Exercise real AX serialization and traversal with a synthetic DTX sink.
    from pymobiledevice3.services.accessibilityaudit import (
        AXAuditElement_v1, AXAuditInspectorFocus_v1, Direction,
    )
    async def exercise():
        audit = api.AccessibilityAudit(SimpleNamespace(product_version="18.0"))
        calls = []
        audit._event_queue = asyncio.Queue()
        async def ready():
            pass
        focus_index = 0
        async def invoke(selector, *args, expects_reply=True):
            nonlocal focus_index
            calls.append((selector, args, expects_reply))
            if selector == "deviceInspectorMoveWithOptions:":
                k = focus_index % 2
                focus_index += 1
                focus = AXAuditInspectorFocus_v1({
                    "CaptionTextValue_v1": f"Synthetic Example {k}, Button",
                    "SpokenDescriptionValue_v1": "Synthetic speech",
                    "ElementValue_v1": AXAuditElement_v1({"PlatformElementValue_v1": bytes([k] * 16)}),
                })
                await audit._event_queue.put(("hostInspectorCurrentElementChanged:", [focus]))
        audit._ensure_ready = ready
        audit._invoke = invoke
        await audit.set_show_visuals(True)
        elements = [item.to_dict() async for item in audit.iter_elements()]
        assert len(elements) == 2  # stops on revisiting a platform identifier
        assert set(elements[0]) == {"caption", "spoken_description", "estimated_uid", "platform_identifier"}
        visuals = calls[0]
        assert visuals == ("deviceInspectorShowVisuals:", (1,), False)
        move = next(call for call in calls if call[0] == "deviceInspectorMoveWithOptions:")
        options = move[1][0]["Value"]
        assert options["direction"]["Value"] == Direction.Next.value
        assert options["includeContainers"]["Value"] == 1
        assert move[2] is False
    asyncio.run(exercise())
