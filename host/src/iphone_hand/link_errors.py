"""Allowlisted link diagnostics; never expose exception text or board replies."""

from .serial_link import ProtocolError


class ReconnectDeferred(ConnectionError):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__('TCP reconnect deferred')


def link_error_class(error: Exception) -> str:
    if isinstance(error, ReconnectDeferred):
        return error.kind if error.kind in {'timeout', 'refused', 'auth', 'closed', 'protocol', 'io'} else 'unavailable'
    if isinstance(error, TimeoutError):
        return 'timeout'
    if isinstance(error, ConnectionRefusedError):
        return 'refused'
    if isinstance(error, ProtocolError):
        return 'auth' if error.code == 'AUTH' else 'protocol'
    if isinstance(error, ConnectionError):
        return 'closed'
    if isinstance(error, OSError):
        return 'io'
    return 'unavailable'
