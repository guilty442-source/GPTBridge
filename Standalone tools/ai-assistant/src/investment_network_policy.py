from __future__ import annotations

import ipaddress
import socket
from typing import Any


_INSTALLED = False
_ORIGINAL_CONNECT: Any = None
_ORIGINAL_CONNECT_EX: Any = None
_ORIGINAL_BIND: Any = None
_ORIGINAL_GETADDRINFO: Any = None


def _is_loopback_host(host: Any) -> bool:
    value = str(host or "").strip().strip("[]").casefold()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _guard_address(address: Any) -> None:
    if not isinstance(address, tuple) or not address or not _is_loopback_host(address[0]):
        raise PermissionError("NETWORK_ACCESS_DENIED_USE_STAR_AI_CHANNEL")


def install_investment_manager_network_policy() -> None:
    """Deny every non-loopback socket operation in the investment manager."""

    global _INSTALLED
    global _ORIGINAL_BIND, _ORIGINAL_CONNECT, _ORIGINAL_CONNECT_EX
    global _ORIGINAL_GETADDRINFO
    if _INSTALLED:
        return
    _INSTALLED = True

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_bind = socket.socket.bind
    original_getaddrinfo = socket.getaddrinfo

    _ORIGINAL_CONNECT = original_connect
    _ORIGINAL_CONNECT_EX = original_connect_ex
    _ORIGINAL_BIND = original_bind
    _ORIGINAL_GETADDRINFO = original_getaddrinfo

    def guarded_connect(instance: socket.socket, address: Any) -> Any:
        _guard_address(address)
        return original_connect(instance, address)

    def guarded_connect_ex(instance: socket.socket, address: Any) -> int:
        _guard_address(address)
        return original_connect_ex(instance, address)

    def guarded_bind(instance: socket.socket, address: Any) -> Any:
        _guard_address(address)
        return original_bind(instance, address)

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_loopback_host(host):
            raise PermissionError("NETWORK_ACCESS_DENIED_USE_STAR_AI_CHANNEL")
        return original_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.socket.bind = guarded_bind
    socket.getaddrinfo = guarded_getaddrinfo


def uninstall_investment_manager_network_policy() -> None:
    """Restore socket operations after the governed runtime has stopped."""

    global _INSTALLED
    global _ORIGINAL_BIND, _ORIGINAL_CONNECT, _ORIGINAL_CONNECT_EX
    global _ORIGINAL_GETADDRINFO
    if not _INSTALLED:
        return

    socket.socket.connect = _ORIGINAL_CONNECT
    socket.socket.connect_ex = _ORIGINAL_CONNECT_EX
    socket.socket.bind = _ORIGINAL_BIND
    socket.getaddrinfo = _ORIGINAL_GETADDRINFO
    _ORIGINAL_CONNECT = None
    _ORIGINAL_CONNECT_EX = None
    _ORIGINAL_BIND = None
    _ORIGINAL_GETADDRINFO = None
    _INSTALLED = False
