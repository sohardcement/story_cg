"""The offline runner must reject external names before DNS or connections."""
import _socket
import socket

import pytest
import p10_05_pytest


@pytest.mark.parametrize('operation', ['resolve', 'connect'])
def test_offline_guard_rejects_external_hostname_before_dns(monkeypatch, operation):
    # Run the actual guard even when the supervisor invokes plain pytest.
    # Track the replaced attributes so this test cannot affect other tests.
    for name in ('create_connection', 'getaddrinfo'):
        monkeypatch.setattr(socket, name, getattr(socket, name))
    for name in ('connect', 'connect_ex'):
        monkeypatch.setattr(socket.socket, name, getattr(socket.socket, name))
    exec(p10_05_pytest.guard_source(), {})
    def forbidden_dns(*args, **kwargs):
        raise AssertionError('External name reached the DNS resolver.')
    monkeypatch.setattr(_socket, 'getaddrinfo', forbidden_dns)
    with pytest.raises(RuntimeError, match='forbids socket connections'):
        if operation == 'resolve':
            socket.getaddrinfo('offline.invalid', 443)
        else:
            socket.create_connection(('offline.invalid', 443))
