"""Seeing whether a plugin's connection followed Reach devices through.

Settings > Network > Reach devices through pins device traffic to one
adapter, and a plugin binds its own connections to ``api.local_address(host)``.
A test sees the bind like this: the "device" listens on this machine's own
network address and the pinned address is 127.0.0.1, so a connection that
binds arrives from 127.0.0.1 and one that does not arrives from the network
address. (A pinned network address would show nothing: a connection from this
machine to itself leaves from that address either way.)
"""

import socket

import pytest

PINNED = "127.0.0.1"


def own_network_address():
    """A non-loopback IPv4 address this machine holds, or None.

    The address the OS would send from toward a documentation-range address
    (connecting a UDP socket sends nothing), confirmed by binding to it.
    """
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))
            ip = probe.getsockname()[0]
        finally:
            probe.close()
        if ip.startswith("127.") or ip == "0.0.0.0":
            return None
        check = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            check.bind((ip, 0))
        finally:
            check.close()
        return ip
    except OSError:
        return None


OWN = own_network_address()
needs_network = pytest.mark.skipif(OWN is None, reason="this machine has no network address")
