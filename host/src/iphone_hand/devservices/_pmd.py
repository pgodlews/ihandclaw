"""Python API adapter for pymobiledevice3 11.26.0 (imports are lazy).

Read: API/selector references are recorded in docs/devservices.md. No CLI,
pairing, privilege escalation, or tunnel creation happens here.
"""

from contextlib import aclosing
from types import SimpleNamespace


class TunnelSelectionError(RuntimeError):
    """Locally generated messages only; never include remote identifiers."""


def load_api():
    from pymobiledevice3.exceptions import AlreadyMountedError
    from pymobiledevice3.services.accessibilityaudit import AccessibilityAudit
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
    from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot
    from pymobiledevice3.services.installation_proxy import InstallationProxyService
    from pymobiledevice3.services.mobile_image_mounter import auto_mount
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.tunneld.api import get_tunneld_tunnels

    return SimpleNamespace(
        AlreadyMountedError=AlreadyMountedError, AccessibilityAudit=AccessibilityAudit,
        DvtProvider=DvtProvider, ProcessControl=ProcessControl, Screenshot=Screenshot,
        InstallationProxyService=InstallationProxyService, auto_mount=auto_mount,
        RemoteServiceDiscoveryService=RemoteServiceDiscoveryService,
        get_tunneld_tunnels=get_tunneld_tunnels,
    )


class PmdBackend:
    def __init__(self, api=None):
        self.api = api
        self.rsd = None
        self.audit = None
        self.visuals = False

    async def connect(self, device_id: str | None) -> None:
        if self.api is None:
            self.api = load_api()
        tunnels = await self.api.get_tunneld_tunnels()
        if device_id is None:
            if not tunnels:
                raise TunnelSelectionError("no active tunnel; check tunneld, trust and Developer Mode")
            if len(tunnels) > 1:
                raise TunnelSelectionError("multiple devices; pass a runtime device selector")
            device_id = next(iter(tunnels))
        # Refuse multiple transports rather than silently selecting a device
        # or allowing the upstream helper to leave extra RSD connections open.
        if not tunnels.get(device_id):
            raise TunnelSelectionError("selected device has no active tunnel")
        if len(tunnels[device_id]) > 1:
            raise TunnelSelectionError("multiple tunnels for selected device; keep one transport active")
        detail = tunnels[device_id][0]
        # The convenience get-by-UDID helper performs a blocking HTTP query in
        # its async body. Use the async listing plus the public RSD constructor,
        # exactly as tunneld's helper does, for a local daemon without blocking
        # the caller's event loop or opening more than one transport.
        self.rsd = self.api.RemoteServiceDiscoveryService(
            (detail["tunnel-address"], detail["tunnel-port"]), name=detail["interface"],
        )
        await self.rsd.connect()

    async def mount(self) -> None:
        try:
            await self.api.auto_mount(self.rsd)
        except self.api.AlreadyMountedError:
            pass

    async def _audit(self):
        if self.audit is None:
            self.audit = self.api.AccessibilityAudit(self.rsd)
            await self.audit.__aenter__()
        return self.audit

    async def iter_items(self):
        audit = await self._audit()
        async with aclosing(audit.iter_elements()) as elements:
            async for element in elements:
                yield element.to_dict()

    async def show_visuals(self, enabled: bool) -> None:
        audit = await self._audit()
        # Mark before sending so that a timed-out request is also cleaned up.
        self.visuals = self.visuals or enabled
        await audit.set_show_visuals(enabled)
        self.visuals = enabled

    async def audit_issues(self, audit_types: list) -> list[dict]:
        audit = await self._audit()
        return [issue.json() for issue in await audit.run_audit(audit_types)]

    async def supported_audit_types(self) -> list:
        return await (await self._audit()).supported_audits_types()

    async def apps(self) -> list[dict]:
        async with self.api.InstallationProxyService(self.rsd) as service:
            return await service.browse(
                options={"ApplicationType": "Any"},
                attributes=["CFBundleIdentifier", "CFBundleDisplayName", "CFBundleName"],
            )

    async def launch(self, bundle_id: str) -> int:
        async with self.api.DvtProvider(self.rsd) as provider:
            async with self.api.ProcessControl(provider) as control:
                # Upstream defaults KillExisting to True; keep the current app
                # alive unless the caller explicitly requests kill/terminate.
                return await control.launch(bundle_id, kill_existing=False)

    async def terminate(self, bundle_id: str) -> bool:
        async with self.api.DvtProvider(self.rsd) as provider:
            async with self.api.ProcessControl(provider) as control:
                pid = await control.process_identifier_for_bundle_identifier(bundle_id)
                if not pid:
                    return False
                await control.kill(pid)
                return True

    async def kill(self, pid: int) -> None:
        async with self.api.DvtProvider(self.rsd) as provider:
            async with self.api.ProcessControl(provider) as control:
                await control.kill(pid)

    async def screenshot(self) -> bytes:
        async with self.api.DvtProvider(self.rsd) as provider:
            async with self.api.Screenshot(provider) as service:
                return await service.get_screenshot()

    async def close(self) -> None:
        # Always close the transport, even if disabling visuals fails.
        try:
            if self.audit is not None:
                try:
                    if self.visuals:
                        await self.audit.set_show_visuals(False)
                finally:
                    await self.audit.close()
        finally:
            self.audit = None
            self.visuals = False
            try:
                if self.rsd is not None:
                    await self.rsd.close()
            finally:
                self.rsd = None
