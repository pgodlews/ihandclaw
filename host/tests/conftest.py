import json
from pathlib import Path
import subprocess
import socket

from PIL import Image
import pytest
import serial

from iphone_hand.fake import FakeSerialDevice, SimulatedScreen
from iphone_hand.phone import Phone
from iphone_hand.serial_link import SerialLink

SCREENS = Path(__file__).resolve().parents[2] / "fixtures/screens"


@pytest.fixture(autouse=True)
def prohibit_hardware(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Tests must not open a serial device or run ADB/subprocess.run")
    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    # Loopback stays allowed: test_tcp_link runs a fake board on 127.0.0.1.
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def loopback_only(real):
        def connect(sock, address, *args):
            host = address[0] if isinstance(address, tuple) else address
            if host not in ("127.0.0.1", "::1", "localhost"):
                forbidden()
            return real(sock, address, *args)
        return connect
    monkeypatch.setattr(socket.socket, "connect", loopback_only(real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", loopback_only(real_connect_ex))


@pytest.fixture
def labels():
    return json.loads((SCREENS / "labels.json").read_text())


@pytest.fixture(params=["home-pointer-notes.png", "home-pointer-notes-at-button.png"])
def real_screen(request):
    with Image.open(SCREENS / request.param) as image:
        return request.param, image.convert("RGB")


@pytest.fixture
def rig(tmp_path):
    device = FakeSerialDevice()
    screen = SimulatedScreen(device)
    phone = Phone(SerialLink(transport=device), screen, settle=0,
                  calibration_path=tmp_path / "calibration.json")
    return phone, device, screen
