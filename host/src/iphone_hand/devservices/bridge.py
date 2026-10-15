"""Async developer-services facade usable by a daemon, independent of Phone.

Read: all public operations are serialized and bounded; failed operations
invalidate the session, and actions are never retried. Errors exclude remote
messages/arguments, which may contain captions or device identifiers.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from io import BytesIO
import math
import re
from time import perf_counter
from typing import Protocol, TypeVar

from PIL import Image

from .captions import Element, parse_caption
from ._pmd import PmdBackend, TunnelSelectionError

T = TypeVar("T")
_BUNDLE = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+\Z")


class Backend(Protocol):
    async def connect(self, device_id: str | None) -> None: ...
    async def mount(self) -> None: ...
    def iter_items(self) -> AsyncGenerator[dict, None]: ...
    async def apps(self) -> list[dict]: ...
    async def launch(self, bundle_id: str) -> int: ...
    async def terminate(self, bundle_id: str) -> bool: ...
    async def kill(self, pid: int) -> None: ...
    async def screenshot(self) -> bytes: ...
    async def show_visuals(self, enabled: bool) -> None: ...
    async def supported_audit_types(self) -> list: ...
    async def audit_issues(self, audit_types: list) -> list[dict]: ...
    async def close(self) -> None: ...


class DevServicesError(RuntimeError):
    """Privacy-safe error. Remote exception text is deliberately omitted."""


@dataclass(frozen=True)
class App:
    bundle_id: str
    display_name: str = field(repr=False)


@dataclass(frozen=True)
class Screenshot:
    data: bytes = field(repr=False)
    width: int
    height: int
    elapsed_ms: float
    format: str | None
    evidence: str = "measured"  # Measured only when this operation actually runs.


def _element(item: dict) -> Element:
    return parse_caption(item.get("caption"), uid=item.get("estimated_uid"),
                         platform_identifier=item.get("platform_identifier"),
                         spoken_description=item.get("spoken_description"))


class DevServices:
    """Use ``async with DevServices() as dev`` on a single event loop.

    Default connect mounts the DDI. ``auto_mount=False`` supports inspecting
    apps without mounting and explicit ``mount()``. Construction/imports are
    inert. ``backend`` allows hardware-free tests. No results are logged/cached.
    Timeout covers each operation, excluding time waiting for another operation.
    """

    def __init__(self, *, device_id: str | None = None, auto_mount: bool = True,
                 timeout: float = 60.0, max_items: int = 512, backend: Backend | None = None):
        if not math.isfinite(timeout) or timeout <= 0 or max_items <= 0:
            raise ValueError("timeout and max_items must be positive")
        self._device_id = device_id
        self._auto_mount = auto_mount
        self._timeout = timeout
        self._max_items = max_items
        self._backend = backend if backend is not None else PmdBackend()
        self._lock = asyncio.Lock()
        self._connected = False

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        # Do not replace an existing body exception with a cleanup error.
        if exc[0] is None:
            await self.close()
        else:
            await self._cleanup()

    async def _cleanup(self):
        self._connected = False
        try:
            await asyncio.wait_for(self._backend.close(), timeout=min(self._timeout, 5.0))
        except Exception:
            pass

    async def _run(self, operation: str, call: Callable[[], Awaitable[T]], *, connecting=False) -> T:
        async with self._lock:
            if not connecting and not self._connected:
                raise DevServicesError("connect before using developer services")
            try:
                return await asyncio.wait_for(call(), timeout=self._timeout)
            except asyncio.CancelledError:
                await self._cleanup()
                raise
            except Exception as exc:
                await self._cleanup()
                if isinstance(exc, DevServicesError):
                    raise
                if isinstance(exc, TunnelSelectionError):
                    raise DevServicesError(str(exc)) from None
                reason = "timed out" if isinstance(exc, TimeoutError) else "failed"
                if isinstance(exc, ImportError):
                    raise DevServicesError("install the devservices extra (pymobiledevice3 11.26.0)") from None
                raise DevServicesError(f"developer services {operation} {reason}; session closed") from None

    async def connect(self) -> None:
        async def operation():
            if self._connected:
                return
            await self._backend.connect(self._device_id)
            if self._auto_mount:
                await self._backend.mount()
            self._connected = True
        await self._run("connect/mount", operation, connecting=True)

    async def close(self) -> None:
        async with self._lock:
            self._connected = False
            try:
                await asyncio.wait_for(self._backend.close(), timeout=min(self._timeout, 5.0))
            except Exception:
                raise DevServicesError("developer services cleanup failed") from None

    async def mount(self) -> None:
        await self._run("mount", self._backend.mount)

    async def list_items(self) -> list[Element]:
        async def operation():
            result = []
            async with aclosing(self._backend.iter_items()) as items:
                async for item in items:
                    if len(result) >= self._max_items:
                        raise DevServicesError("accessibility item limit exceeded; session closed")
                    result.append(_element(item))
            return result
        return await self._run("items", operation)

    async def list_apps(self) -> list[App]:
        async def operation():
            return sorted((App(item["CFBundleIdentifier"],
                               item.get("CFBundleDisplayName") or item.get("CFBundleName")
                               or item["CFBundleIdentifier"])
                           for item in await self._backend.apps() if item.get("CFBundleIdentifier")),
                          key=lambda app: (app.display_name.casefold(), app.bundle_id))
        return await self._run("apps", operation)

    async def resolve_bundle(self, bundle_or_name: str) -> str:
        """Exact, case-insensitive display-name lookup; ambiguity is an error."""
        if _BUNDLE.fullmatch(bundle_or_name):
            return bundle_or_name
        apps = await self.list_apps()
        bundles = {app.bundle_id for app in apps if app.display_name.casefold() == bundle_or_name.strip().casefold()}
        if len(bundles) != 1:
            raise DevServicesError("app name is missing or ambiguous; use a bundle identifier")
        return bundles.pop()

    async def launch(self, bundle_id: str) -> int:
        if not _BUNDLE.fullmatch(bundle_id):
            raise ValueError("launch requires a bundle identifier; call resolve_bundle for a name")
        return await self._run("launch", lambda: self._backend.launch(bundle_id))

    async def terminate(self, bundle_id: str) -> bool:
        """SIGKILL the process resolved by bundle; False means it was not running."""
        if not _BUNDLE.fullmatch(bundle_id):
            raise ValueError("terminate requires a bundle identifier")
        return await self._run("terminate", lambda: self._backend.terminate(bundle_id))

    async def kill(self, pid: int) -> None:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("pid must be a positive integer")
        await self._run("kill", lambda: self._backend.kill(pid))

    async def screenshot(self) -> Screenshot:
        async def operation():
            start = perf_counter()
            data = await self._backend.screenshot()
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
                image_format = image.format
                image.verify()
            return Screenshot(data, width, height, (perf_counter() - start) * 1000, image_format)
        return await self._run("screenshot", operation)

    async def show_visuals(self, enabled: bool) -> None:
        await self._run("visuals", lambda: self._backend.show_visuals(enabled))

    async def focus(self, uid: str, *, highlight: bool = True) -> Element:
        """Traverse until a UID/platform identifier is focused; no direct UID setter.

        The iterator pauses at the matched element before advancing again.
        Highlight is a fire-and-forget request, not proof the overlay appeared.
        ``show_visuals(False)`` or ``close()`` requests overlay cleanup.
        """
        async def operation():
            await self._backend.show_visuals(highlight)
            count = 0
            async with aclosing(self._backend.iter_items()) as items:
                async for item in items:
                    count += 1
                    if count > self._max_items:
                        break
                    element = _element(item)
                    if uid and uid in (element.uid, element.platform_identifier):
                        return element
            raise DevServicesError("focus target unavailable; session closed")
        return await self._run("focus", operation)

    async def supported_audit_types(self) -> list:
        return await self._run("audit types", self._backend.supported_audit_types)

    async def audit_issues(self, audit_types: list) -> list[dict]:
        """Issue-only rect strings in upstream JSON; not complete element geometry.

        Audit types must be explicitly chosen by the caller. Audits may change
        the inspected UI; no audit runs during list_items or OCR matching.
        """
        return await self._run("audit", lambda: self._backend.audit_issues(audit_types))
