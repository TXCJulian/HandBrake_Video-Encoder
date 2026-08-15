"""Request body size limiting.

``EncodeRequest.preset_json`` is an unbounded dict: without a cap, a request
body is read into memory and parsed by Pydantic before any route check runs, so
the path allowlist and encoder checks offer no protection against a large one.

Implemented as raw ASGI rather than a Starlette ``BaseHTTPMiddleware`` because
the check must happen *before* the body is buffered -- a middleware that awaits
``request.body()`` has already paid the cost it is meant to prevent.
"""

import json
from typing import Any, Callable, Awaitable

_TOO_LARGE = 413


class BodySizeLimitMiddleware:
    """Reject request bodies larger than *max_bytes*.

    Two layers, because they fail differently:

    * ``Content-Length``, when present, is checked before the application is
      invoked at all, which yields a clean ``413`` with the service's usual
      ``code``/``reason`` body. Every ordinary client sends it.
    * Bodies without a usable ``Content-Length`` (chunked transfer encoding, or
      a lying header) are counted as they stream. Once the limit is passed the
      request is cut off, because by then the application is mid-request and a
      tidy response can no longer be substituted. Memory stays bounded, which
      is the point; the client sees a dropped request rather than a 413.
    """

    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await _reject(send, declared, self.max_bytes)
            return

        seen = 0

        async def limited_receive() -> dict:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    # Cannot send a 413 here without racing whatever the app is
                    # already sending, so the request is severed instead. The
                    # body is never accumulated past the limit either way.
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, limited_receive, send)


def _content_length(scope: dict) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _reject(send: Callable[[dict], Awaitable[None]], size: int, limit: int) -> None:
    body = json.dumps({
        "code": "request_too_large",
        "reason": f"Request body is {size} bytes; the limit is {limit}.",
    }).encode()
    await send({
        "type": "http.response.start",
        "status": _TOO_LARGE,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})
