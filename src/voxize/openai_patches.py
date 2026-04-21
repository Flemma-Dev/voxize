"""Monkey-patch for openai SDK Stream to allow HTTP connection pool reuse.

Problem: ``openai._streaming.Stream.__stream__`` ``break``s out of its SSE
loop when it sees ``data: [DONE]``. httpx marks an HTTP/1.1 connection
poolable only if the body has been fully consumed — and breaking early
leaves the chunked-transfer terminator bytes unread. Endpoints that emit
``[DONE]`` (notably ``/v1/audio/transcriptions``) end up closing their
connection instead of returning it to the pool, so subsequent calls
(e.g., ``/v1/responses`` for cleanup) pay a fresh TCP+TLS handshake.

Fix: swap the ``break`` for a ``continue``. The SSE decoder keeps reading
until the underlying body iterator naturally exhausts; httpx sees the body
as fully drained; the connection is returned to the pool.

To remove: delete the ``install()`` call from ``__main__.py`` and this file.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from openai._exceptions import APIError
from openai._streaming import Stream
from openai._utils import is_mapping

logger = logging.getLogger(__name__)

_installed = False


def install() -> None:
    """Apply the patch. Idempotent."""
    global _installed
    if _installed:
        return

    def _patched_stream(self):
        # Copy of openai._streaming.Stream.__stream__ with one behaviour
        # change: `continue` instead of `break` on [DONE].
        cast_to = cast(Any, self._cast_to)
        response = self.response
        process_data = self._client._process_response_data
        iterator = self._iter_events()
        try:
            for sse in iterator:
                if sse.data.startswith("[DONE]"):
                    continue
                if sse.event and sse.event.startswith("thread."):
                    data = sse.json()
                    if sse.event == "error" and is_mapping(data) and data.get("error"):
                        error = data.get("error")
                        message = None
                        if is_mapping(error):
                            message = error.get("message")
                        if not message or not isinstance(message, str):
                            message = "An error occurred during streaming"
                        raise APIError(
                            message=message,
                            request=self.response.request,
                            body=data["error"],
                        )
                    yield process_data(
                        data={"data": data, "event": sse.event},
                        cast_to=cast_to,
                        response=response,
                    )
                else:
                    data = sse.json()
                    if is_mapping(data) and data.get("error"):
                        error = data.get("error")
                        message = None
                        if is_mapping(error):
                            message = error.get("message")
                        if not message or not isinstance(message, str):
                            message = "An error occurred during streaming"
                        raise APIError(
                            message=message,
                            request=self.response.request,
                            body=data["error"],
                        )
                    opts = getattr(self, "_options", None)
                    synthesize = bool(
                        opts is not None
                        and getattr(opts, "synthesize_event_and_data", False)
                    )
                    yield process_data(
                        data=(
                            {"data": data, "event": sse.event} if synthesize else data
                        ),
                        cast_to=cast_to,
                        response=response,
                    )
        finally:
            response.close()

    Stream.__stream__ = _patched_stream
    _installed = True
    logger.debug("openai_patches: Stream.__stream__ patched for pool reuse")
