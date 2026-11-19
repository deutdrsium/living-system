"""City navigation tests may use synthetic transports and loopback only."""
import ipaddress
import socket

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "timing: sensitive to scheduling and wall-clock deadlines")


def _loopback(host):
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def city_network_guard(request, monkeypatch):
    if not request.node.path.name.startswith(("test_amap", "test_city", "test_w6_scenario")):
        return

    def check(host):
        if not _loopback(host):
            raise AssertionError("Non-loopback network forbidden in city tests")

    def guard_socket(method):
        def guarded(sock, address, *args, **kwargs):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                check(address[0])
            return method(sock, address, *args, **kwargs)
        return guarded

    for name in ("connect", "connect_ex", "bind"):
        monkeypatch.setattr(socket.socket, name, guard_socket(getattr(socket.socket, name)))

    sendto = socket.socket.sendto

    def guarded_sendto(sock, data, *args):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            check(args[-1][0])
        return sendto(sock, data, *args)

    monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)
    if hasattr(socket.socket, "sendmsg"):
        sendmsg = socket.socket.sendmsg

        def guarded_sendmsg(sock, buffers, ancdata=(), flags=0, address=None):
            if address is None:
                return sendmsg(sock, buffers, ancdata, flags)
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                check(address[0])
            return sendmsg(sock, buffers, ancdata, flags, address)

        monkeypatch.setattr(socket.socket, "sendmsg", guarded_sendmsg)

    def guard_dns(method):
        def guarded(host, *args, **kwargs):
            check(host)
            return method(host, *args, **kwargs)
        return guarded

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        monkeypatch.setattr(socket, name, guard_dns(getattr(socket, name)))

    getnameinfo = socket.getnameinfo

    def guarded_getnameinfo(address, flags):
        check(address[0])
        return getnameinfo(address, flags | socket.NI_NUMERICHOST)

    monkeypatch.setattr(socket, "getnameinfo", guarded_getnameinfo)
