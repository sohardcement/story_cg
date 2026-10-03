"""Run offline pytest with socket connections rejected, including subprocesses.

P10_05_ALLOW_LOOPBACK=1 permits the suite's local HTTP fixture; external
addresses stay blocked. Default runs reject every socket connection.
"""
import os
from pathlib import Path
import subprocess
import sys


def guard_source():
    return '''import os, socket
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_create_connection = socket.create_connection
_getaddrinfo = socket.getaddrinfo
def reject_network(*args, **kwargs):
    raise RuntimeError("P10-05 offline validation forbids socket connections")
def allow_loopback(host):
    return os.environ.get("P10_05_ALLOW_LOOPBACK") == "1" and host in ("127.0.0.1", "::1")
def guarded_connect(sock, address, *, original=_connect):
    if isinstance(address, tuple) and allow_loopback(address[0]):
        return original(sock, address)
    return reject_network()
def guarded_connect_ex(sock, address):
    return guarded_connect(sock, address, original=_connect_ex)
def guarded_create_connection(address, *args, **kwargs):
    if allow_loopback(address[0]):
        return _create_connection(address, *args, **kwargs)
    return reject_network()
def guarded_getaddrinfo(host, *args, **kwargs):
    if allow_loopback(host):
        return _getaddrinfo(host, *args, **kwargs)
    return reject_network()
socket.socket.connect = guarded_connect
socket.socket.connect_ex = guarded_connect_ex
socket.create_connection = guarded_create_connection
socket.getaddrinfo = guarded_getaddrinfo
'''


def main():
    root = Path(__file__).resolve().parents[1]
    guard = root/'reports/assets/P10-05/network-guard'
    guard.mkdir(parents=True, exist_ok=True)
    (guard/'sitecustomize.py').write_text(guard_source())
    env = dict(os.environ)
    env['PYTHONPATH'] = str(guard)+os.pathsep+env.get('PYTHONPATH', '')
    return subprocess.call([sys.executable, '-m', 'pytest', *sys.argv[1:]], cwd=root, env=env)


if __name__ == '__main__':
    raise SystemExit(main())
