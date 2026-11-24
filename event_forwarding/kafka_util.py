"""Broker reachability probe (plain socket connect, no event handling)."""
from __future__ import annotations

import socket


class BrokerUnreachableError(RuntimeError):
    """None of the bootstrap servers accepted a TCP connection."""


def parse_bootstrap_servers(bootstrap_servers: str) -> list[tuple[str, int]]:
    """Split ``host:port[,host:port...]`` into (host, port) pairs. Raises ValueError if malformed."""
    pairs: list[tuple[str, int]] = []
    for item in bootstrap_servers.split(","):
        item = item.strip()
        if not item:
            continue
        host, sep, port = item.rpartition(":")
        if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f"invalid bootstrap server {item!r}, expected host:port")
        pairs.append((host.strip("[]"), int(port)))
    if not pairs:
        raise ValueError("no bootstrap servers given")
    return pairs


def probe_bootstrap_servers(bootstrap_servers: str, timeout: float = 3.0) -> bool:
    """Return True when at least one bootstrap server accepts a TCP connection."""
    for host, port in parse_bootstrap_servers(bootstrap_servers):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def require_broker(bootstrap_servers: str, timeout: float = 3.0) -> None:
    """Raise ``BrokerUnreachableError`` naming the bootstrap servers when none is reachable."""
    if not probe_bootstrap_servers(bootstrap_servers, timeout):
        raise BrokerUnreachableError(f"Kafka broker not reachable at {bootstrap_servers}")
