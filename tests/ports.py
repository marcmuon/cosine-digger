"""Loopback ports for tests: from DIGGER_TEST_PORTS (for example 18800-18899)
when it is set, otherwise any free port the OS picks. Never 8777."""
from __future__ import annotations

import os
import socket


def test_port_range() -> range | None:
    spec = os.environ.get("DIGGER_TEST_PORTS", "").strip()
    if not spec:
        return None
    low, _, high = spec.partition("-")
    return range(int(low), int(high or low) + 1)


def bind_loopback(sock: socket.socket) -> int:
    """Bind ``sock`` to 127.0.0.1 on a test port and return the port."""
    ports = test_port_range()
    if ports is None:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        assert port != 8777
        return port
    for port in ports:
        if port == 8777:
            continue
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            continue
        return port
    raise RuntimeError("No free port left in DIGGER_TEST_PORTS")
