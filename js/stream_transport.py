"""Own HTTP response iterators until the SDK closes their response.

httpx2 2.12's default response stream closes the pool entry but not its active
byte iterator (pydantic/httpx2#1195). This response-local wrapper closes both.
Recheck the adapter when that upstream iterator ownership is repaired.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx2
from httpx2._client import BoundAsyncStream
from httpx2._transports.default import AsyncResponseStream


class _OwnedCoreStream:
    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._iterator: AsyncIterator[bytes] = aiter(stream)
        self._closed = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        return await anext(self._iterator)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if isinstance(self._iterator, AsyncGenerator):
                await self._iterator.aclose()
        finally:
            await self._stream.aclose()


@asynccontextmanager
async def own_responses(client: httpx2.AsyncClient):
    """Close this model request's byte iterators when its stream scope exits."""
    async with AsyncExitStack() as cleanup:

        async def own(response: httpx2.Response) -> None:
            stream: AsyncIterable[bytes] = response.stream
            if isinstance(stream, BoundAsyncStream):
                stream = stream._stream
            if isinstance(stream, AsyncResponseStream):
                wrapper = _OwnedCoreStream(stream._httpcore_stream)
                stream._httpcore_stream = wrapper
                cleanup.push_async_callback(wrapper.aclose)

        hooks = client.event_hooks["response"]
        hooks.append(own)
        try:
            yield
        finally:
            hooks.remove(own)
