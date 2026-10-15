"""Official MCP SDK stdio server: thin wrappers over the shared Phone API."""

import argparse
import asyncio
import base64
from functools import wraps
from io import BytesIO
import json
import threading

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
from PIL import ImageDraw

from .cli import connection_options, make_phone
from .phone import Phone


def create_server(phone=None, *, threaded: bool = False, **settings) -> FastMCP:
    phone = phone if phone is not None else Phone()
    server = FastMCP("iphone-hand", **settings)
    capture_lock = threading.Lock()

    def tool(**options):
        def register(fn):
            if threaded:
                original = fn
                @wraps(original)
                async def fn(*args, **kwargs):
                    return await asyncio.to_thread(original, *args, **kwargs)
            return server.tool(**options)(fn)
        return register

    @tool(structured_output=False)
    def screenshot(mark_pointer: bool = True) -> list[ImageContent | TextContent]:
        """PNG at 2 px/pt (750x1624 for 375x812 pt; see status screen.pt) and the pointer in points."""
        with capture_lock:
            # The standard Phone API is sufficient for third-party doubles;
            # our optional extension gets metadata atomically from one frame.
            if hasattr(phone, "screenshot_with_pointer"):
                image, position = phone.screenshot_with_pointer()
            else:
                image, position = phone.screenshot(), phone.find_pointer()
            if mark_pointer and position is not None:
                x, y = position[0] * 2, position[1] * 2
                draw = ImageDraw.Draw(image)
                draw.ellipse((x - 36, y - 36, x + 36, y + 36), outline="cyan", width=3)
                draw.line((x - 8, y, x + 8, y), fill="cyan", width=2)
                draw.line((x, y - 8, x, y + 8), fill="cyan", width=2)
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            metadata = dict(zip(("x", "y", "confidence"), position)) if position is not None else None
            return [ImageContent(type="image", mimeType="image/png",
                                 data=base64.b64encode(buffer.getvalue()).decode("ascii")),
                    TextContent(type="text", text=json.dumps({"pointer": metadata}))]

    @tool()
    def find_pointer() -> dict | None:
        """Return {x,y,confidence} in phone points or null when unavailable."""
        position = phone.find_pointer()
        return dict(zip(("x", "y", "confidence"), position)) if position is not None else None

    @tool()
    def move_to(x: float, y: float) -> dict:
        """Move visually; return final x,y,error_pt,iterations (at most six)."""
        return phone.move_to(x, y)

    @tool()
    def tap(x: float, y: float, button: str = "left") -> dict:
        """Move to a phone point and click left, right or middle."""
        return phone.tap(x, y, button)

    @tool()
    def long_press(x: float, y: float, ms: int = 800) -> dict:
        """Move to a phone point and hold the left button."""
        return phone.long_press(x, y, ms)

    @tool()
    def drag(x1: float, y1: float, x2: float, y2: float, steps: int = 10) -> dict:
        """Drag between phone points, always releasing the held button."""
        return phone.drag(x1, y1, x2, y2, steps)

    @tool()
    def scroll(dy: int, dx: int = 0, x: float | None = None, y: float | None = None) -> dict | None:
        """Scroll wheel units, optionally first positioning at (x,y); returns pointer recovery metadata if any."""
        return phone.scroll(dy, dx, x, y)

    @tool()
    def type_text(text: str) -> dict:
        """Type text using the US hardware keyboard layout."""
        return phone.type_text(text)

    @tool()
    def key(combo: str) -> None:
        """Press a combo such as cmd+space, cmd+shift+3, enter or an arrow."""
        return phone.key(combo)

    @tool()
    def home() -> None:
        """Configured Home action: cmd+h by default, or button3."""
        return phone.home()

    @tool()
    def status() -> dict:
        """Serial/BLE state, screen geometry in points and capture timings; no capture performed."""
        return phone.status()

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="iphone-hand-mcp")
    connection_options(parser)
    args = parser.parse_args(argv)
    phone = make_phone(args)
    try:
        create_server(phone).run(transport="stdio")
    finally:
        phone.close()


if __name__ == "__main__":
    main()
