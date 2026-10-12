import asyncio
import base64
from io import BytesIO
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image
import pytest

from iphone_hand.cli import main


@pytest.mark.parametrize("args", [["status"], ["pointer"], ["move", "10", "20"],
    ["tap", "10", "20"], ["type", "Hello"], ["key", "cmd+space"], ["calibrate"]])
def test_cli_fake_commands(args, capsys):
    assert main(["--fake", *args]) == 0
    json.loads(capsys.readouterr().out)


def test_cli_fake_screenshot(tmp_path, capsys):
    output = tmp_path / "shot.png"
    assert main(["--fake", "shot", str(output)]) == 0
    with Image.open(output) as image:
        assert image.size == (750, 1624)


def test_cli_errors(capsys):
    assert main(["--fake", "move", "-1", "2"]) == 1
    assert "target" in capsys.readouterr().err


def test_official_sdk_stdio_round_trip():
    # Subprocess is exclusively our --fake stdio server, with no device paths.
    async def exercise():
        parameters = StdioServerParameters(command=sys.executable,
                                          args=["-m", "iphone_hand.mcp_server", "--fake"])
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name: t for t in (await session.list_tools()).tools}
                assert set(tools) == {"screenshot", "find_pointer", "move_to", "tap", "long_press",
                                     "drag", "scroll", "type_text", "key", "home", "status"}
                assert tools["screenshot"].inputSchema["properties"]["mark_pointer"]["default"] is True
                shot = await session.call_tool("screenshot", {"mark_pointer": False})
                assert not shot.isError
                block = next(c for c in shot.content if c.type == "image")
                with Image.open(BytesIO(base64.b64decode(block.data))) as image:
                    assert image.size == (750, 1624)
                    assert image.getpixel((375, 812)) == (255, 255, 255)
                metadata = next(c for c in shot.content if c.type == "text")
                assert json.loads(metadata.text)["pointer"]["x"] == 187.5
                marked = await session.call_tool("screenshot", {})
                block = next(c for c in marked.content if c.type == "image")
                with Image.open(BytesIO(base64.b64decode(block.data))) as image:
                    assert image.getpixel((375, 812)) == (0, 255, 255)
                for name, args in [
                    ("move_to", {"x": 100, "y": 200}), ("tap", {"x": 100, "y": 200}),
                    ("long_press", {"x": 100, "y": 200}),
                    ("drag", {"x1": 1, "y1": 2, "x2": 10, "y2": 20}),
                    ("scroll", {"dy": 1}), ("type_text", {"text": "Hello"}),
                    ("key", {"combo": "cmd+space"}), ("home", {}),
                    ("find_pointer", {}), ("status", {})]:
                    assert not (await session.call_tool(name, args)).isError
                bad = await session.call_tool("move_to", {"x": -1, "y": 200})
                assert bad.isError
    asyncio.run(exercise())
