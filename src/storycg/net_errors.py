"""V2-35: the failures the story entry answers with needs_user (a network or resource problem), not a traceback.

Anything outside RESOURCE_FAILURES is a program error and keeps raising.
"""
import socket
import ssl

import httpx


class ResponseBodyError(httpx.HTTPError, ValueError):
    """A response came back but its body is not the JSON the caller expects (truncated, HTML, wrong type).

    It is an httpx.HTTPError, so the handlers for network failures take it; it is also a ValueError, so the
    places that already catch a bad body as ValueError keep doing so.
    """


def json_body(response: httpx.Response):
    """response.json(), with a body that does not parse raised as ResponseBodyError."""
    try:
        return response.json()
    except ValueError as exc:
        raise ResponseBodyError(f'响应体不是预期的 JSON：{exc}') from exc


RESOURCE_FAILURES = (httpx.HTTPError, ConnectionError, TimeoutError, socket.gaierror, ssl.SSLError)
