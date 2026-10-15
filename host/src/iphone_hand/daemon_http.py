"""One authenticated ASGI app for the web UI, REST, MJPEG and official MCP HTTP."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from mcp.server.transport_security import TransportSecuritySettings

from .airplay import FrameUnavailable
from .daemon_control import Controller, Paused, PasswordPrompt
from .mcp_server import create_server
from .screen import PUBLIC_SIZE
from .motion import PointerHidden
from .phone import TypeIncomplete


def jpeg(image):
    buffer = BytesIO()
    image.save(buffer, 'JPEG', quality=85)
    return buffer.getvalue()


def mjpeg_part(data):
    return b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(data)).encode()+b'\r\n\r\n'+data+b'\r\n'


async def mjpeg_frames(screen, disconnected, *, sleeper=asyncio.sleep, size=None):
    """`size(frame)` gives the pt2 size (2 px per pt); default: the 375x812 reference."""
    while not await disconnected():
        started = asyncio.get_running_loop().time()
        try:
            def frame():
                image = screen.grab()
                return jpeg(image.resize(size(image) if size else PUBLIC_SIZE))
            yield mjpeg_part(await asyncio.to_thread(frame))
        except FrameUnavailable:
            pass  # Keep connection alive while the receiver recovers; never send stale images.
        await sleeper(max(0.1, 0.1-(asyncio.get_running_loop().time()-started)))


class TokenAuth:
    """Pure ASGI middleware also protects mounted SDK routes and unknown paths."""
    def __init__(self, app, token):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        bearer = headers.get(b'authorization', b'')
        query = parse_qs(scope.get('query_string', b'').decode('utf-8', errors='replace'))
        supplied = bearer[7:] if bearer.startswith(b'Bearer ') else query.get('token', [''])[0].encode()
        if not hmac.compare_digest(supplied, self.token):
            return await JSONResponse({'error': 'Authentication required'}, 401,
                                      headers={'WWW-Authenticate': 'Bearer'})(scope, receive, send)
        origin = headers.get(b'origin')
        if origin:
            source = urlsplit(origin.decode())
            if source.netloc.lower() != headers.get(b'host', b'').decode().lower() or source.scheme != scope['scheme']:
                return await JSONResponse({'error': 'Cross-origin request refused'}, 403)(scope, receive, send)
        # Bound input without trusting Content-Length; includes chunked request bodies.
        size = 0
        async def bounded_receive():
            nonlocal size
            message = await receive()
            size += len(message.get('body', b''))
            if size > 65536:
                raise ValueError('Request body exceeds limit')
            return message
        async def private_send(message):
            if message['type'] == 'http.response.start':
                message['headers'] = list(message.get('headers', []))+[
                    (b'cache-control', b'no-store'), (b'referrer-policy', b'no-referrer'),
                    (b'x-content-type-options', b'nosniff')]
            await send(message)
        return await self.app(scope, bounded_receive, private_send)


def create_app(config, phone, screen, elements, supervisor=None, receiver=None):
    controller = Controller(phone, screen, elements, receiver if receiver is not None else supervisor)
    controller.receiver_name = config.uxplay_name
    controller.frame_source = getattr(config, 'frame_source', 'airplay')
    # Existing section-3 definitions, executed in worker threads so pause stays responsive.
    mcp = create_server(controller, threaded=True, stateless_http=True, json_response=True,
                        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    def tool(fn):
        from functools import wraps
        @wraps(fn)
        async def threaded(*args, **kwargs):
            return await asyncio.to_thread(fn, *args, **kwargs)
        mcp.tool()(threaded)
        return fn

    @tool
    def elements() -> list[dict]:
        """Voice Control targets and OCR text in phone points."""
        return controller.elements()['elements']

    @tool
    def tap_element(id: str) -> dict:
        """Tap a current element by id (vc number or OCR text id)."""
        return controller.action('tap_element', id=id)

    @tool
    def tap_text(text: str, nth: int = 0) -> dict:
        """Tap the best text match, choosing nth among matching elements."""
        return controller.action('tap_text', text=text, nth=nth)

    @tool
    def launch(app: str) -> dict:
        """Open Spotlight, safely type an app name, and press Enter."""
        return controller.action('launch', app=app)

    @tool
    def back() -> dict:
        """Estimated left-edge swipe; app support varies."""
        return controller.action('back')

    @tool
    def wait_for(text: str, timeout: float = 10) -> dict:
        """Wait for matching text, with a bounded timeout."""
        return controller.action('wait_for', text=text, timeout=timeout)

    @tool
    def scroll_until(text: str, timeout: float = 10, dy: int = -3, max_scrolls: int = 10) -> dict:
        """Scroll until text appears, with bounded time and scroll count."""
        return controller.action('scroll_until', text=text, timeout=timeout, dy=dy, max_scrolls=max_scrolls)

    @tool
    def run_shortcut(name: str) -> dict:
        """Launch a named shortcut via Spotlight (best effort)."""
        return controller.action('run_shortcut', name=name)

    sdk_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        try:
            if supervisor:
                await supervisor.start()
            async with mcp.session_manager.run():
                yield
        finally:
            if supervisor:
                await supervisor.stop()
            if hasattr(screen, 'close'):
                await asyncio.to_thread(screen.close)
            await asyncio.to_thread(phone.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.controller, app.state.mcp = controller, mcp
    app.add_middleware(TokenAuth, token=config.token)

    @app.get('/')
    def index():
        return HTMLResponse(Path(__file__).with_name('static').joinpath('index.html').read_text())

    @app.get('/api/frame.jpg')
    def frame(scale: str | None = None):
        try:
            image = screen.grab(newest=True) if hasattr(screen, 'files') else screen.grab()
            if scale not in (None, 'pt2'):
                return JSONResponse({'error': 'scale must be pt2'}, 422)
            if scale == 'pt2':
                image = image.resize(controller.public_size(image))
            return Response(jpeg(image), media_type='image/jpeg')
        except FrameUnavailable:
            return JSONResponse({'error': 'No fresh AirPlay frame'}, 503)

    @app.get('/api/stream.mjpeg')
    async def stream(request: Request):
        return StreamingResponse(mjpeg_frames(screen, request.is_disconnected, size=controller.public_size),
                                 media_type='multipart/x-mixed-replace; boundary=frame')

    state_task = None

    @app.get('/api/state')
    async def state():
        nonlocal state_task
        # Read: DNS and waiting for the controller lock are outside socket
        # timeouts. Share one pending read so slow polls cannot pile up workers.
        if state_task is None or state_task.done():
            state_task = asyncio.create_task(asyncio.to_thread(controller.state))
            state_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        try:
            return await asyncio.wait_for(asyncio.shield(state_task), timeout=2.5)
        except TimeoutError:
            return {**controller.cached_state(),
                    'link': {'connected': False, 'error': 'timeout'},
                    'ble': {'conn': False, 'ready': False}}

    @app.get('/api/elements')
    def get_elements(annotate: bool = False):
        try:
            frame = screen.grab()
            result = controller.analyze(frame)
            if annotate:
                return Response(jpeg(controller.detector.annotate(frame, result)), media_type='image/jpeg')
            return JSONResponse(result['elements'], headers={
                'X-Elements-Latency-Ms': str(result['latency_ms']),
                'X-VC-Tap-Offset-Pt': json.dumps(result['tap_offset_pt']),
                'X-OCR-Available': str(result['ocr_available']).lower(),
                'X-Pointer-Pt': json.dumps(result['pointer']),
            })
        except FrameUnavailable:
            return JSONResponse({'error': 'No fresh AirPlay frame'}, 503)
        except Exception:
            return JSONResponse({'error': 'Element analysis unavailable'}, 503)

    @app.post('/api/pause')
    async def pause(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {'paused'}:
                raise ValueError
            # Immediate event update: no Phone lock, status grab, or serial command here.
            return controller.set_paused(body['paused'])
        except (ValueError, TypeError):
            return JSONResponse({'error': 'Expected {paused: boolean}'}, 422)

    def action_route(action):
        def action_error(payload, status):
            if action == 'type':
                payload.setdefault('typed', 0)
            return JSONResponse(payload, status)

        async def endpoint(request: Request):
            try:
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError
                return await asyncio.to_thread(controller.action, action, **body)
            except TypeIncomplete as exc:
                status = 423 if isinstance(exc.cause, Paused) else 504 if isinstance(exc.cause, TimeoutError) else 503
                return action_error({'error': 'Typing incomplete; inspect state before resuming',
                                     'typed': exc.typed, 'sent': exc.sent,
                                     'confirmed_indices': list(exc.confirmed_indices)}, status)
            except Paused:
                return action_error({'error': 'Actions are paused'}, 423)
            except PasswordPrompt:
                return action_error({'error': 'Typing refused: password prompt or OCR unavailable'}, 409)
            except FrameUnavailable:
                return action_error({'error': 'No fresh AirPlay frame'}, 503)
            except PointerHidden as exc:
                payload = {'error': PointerHidden.message, 'error_class': 'PointerHidden'}
                if exc.recovered_with:
                    payload['recovered_with'] = exc.recovered_with
                return action_error(payload, 503)
            except LookupError:
                return action_error({'error': 'Target not found'}, 404)
            except TimeoutError:
                return action_error({'error': 'Operation timed out; inspect state before retrying'}, 504)
            except (ValueError, TypeError):
                return action_error({'error': 'Invalid action arguments'}, 422)
            except Exception:
                return action_error({'error': 'Action failed; inspect state before retrying'}, 503)
        return endpoint

    for action in sorted(controller.ACTIONS):
        app.add_api_route(f'/api/{action}', action_route(action), methods=['POST'])
    # Preserve exact /mcp path (no mount redirect) and official ASGI protocol handling.
    app.router.routes.extend(sdk_app.routes)
    return app
