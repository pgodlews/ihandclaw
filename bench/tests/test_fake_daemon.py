"""Unit tests for FakeDaemon and FakeDaemonServer (offline, zero-network / loopback)."""

import pytest

from bench.fake_daemon import FakeDaemon, FakeDaemonServer
from bench.daemon_client import DaemonClient, DaemonError


def test_fake_daemon_in_process_zero_network():
    daemon = FakeDaemon()
    client = DaemonClient(fake_daemon=daemon)

    state = client.get_state()
    assert state["mirroring"] is True
    assert state["current_app"] == "Home"

    elements = client.get_elements()
    assert len(elements) >= 5
    app_names = [e["text"] for e in elements]
    assert "Settings" in app_names

    # Test launching Settings
    client.launch("Settings")
    settings_elements = client.get_elements()
    texts = [e["text"] for e in settings_elements]
    assert "Settings" in texts
    assert "Display & Brightness" in texts

    # Navigate to Display & Brightness
    client.tap_text("Display & Brightness")
    disp_elements = client.get_elements()
    disp_texts = [e["text"] for e in disp_elements]
    assert "Appearance" in disp_texts
    assert "Dark" in disp_texts

    # Toggle dark mode
    client.tap_text("Dark")
    state_after = client.get_state()
    assert state_after["dark_mode"] is True

    # Check frame reflects dark background
    frame = client.get_frame()
    assert frame.size == (1126, 2434)
    # Reset
    client.home()
    assert client.get_state()["current_app"] == "Home"


def test_fake_daemon_server_loopback_and_auth():
    try:
        server = FakeDaemonServer(required_token="test-token-1234")
    except PermissionError:
        pytest.skip("Sandbox forbids loopback socket binding; in-process tests still run")
    server.start()
    try:
        # Unauthenticated request should raise DaemonError
        unauth_client = DaemonClient(base_url=server.url)
        with pytest.raises(DaemonError) as exc_info:
            unauth_client.get_state()
        assert "401" in str(exc_info.value)

        # Authenticated client succeeds
        auth_client = DaemonClient(base_url=server.url, token="test-token-1234")
        state = auth_client.get_state()
        assert state["mirroring"] is True

        # Test action dispatch
        res = auth_client.launch("Calculator")
        assert res["ok"] is True
        calc_elements = auth_client.get_elements()
        calc_texts = [e["text"] for e in calc_elements]
        assert "AC" in calc_texts
        assert "=" in calc_texts
    finally:
        server.stop()
