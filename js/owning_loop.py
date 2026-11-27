"""A coroutine run to its end on an event loop of its own, with the SDK's asyncgen-teardown noise filtered from that loop's exception handler."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Callable
from typing import Any

# The loop's own teardown protocol, not anybody's failure. `shutdown_asyncgens`
# throws GeneratorExit into every async generator still suspended at a yield; a
# generator whose teardown awaits (or whose teardown is itself another generator
# being closed in the same pass) cannot stop on that throw, and the close fails
# with one of these. Every one of them is raised BY the close protocol, after the
# work already succeeded.
_ASYNCGEN_SHUTDOWN_MESSAGE = "an error occurred during closing of asynchronous generator"


_ASYNCGEN_PROTOCOL_ERRORS = (
    "didn't stop after athrow()",
    "asynchronous generator is already running",
    "async generator is already running",
    "ignored GeneratorExit",
)


def install_asyncgen_shutdown_filter(loop: asyncio.AbstractEventLoop) -> None:
    """Arm the filter below on a loop js owns, immediately before shutting it down.

    Every sync entry point that runs a turn ends by closing its own loop, and
    every one of them hits the same upstream teardown, so each of them installs
    this. Arming it late keeps a running loop's own error reporting untouched.
    """
    loop.set_exception_handler(_asyncgen_shutdown_filter(loop.get_exception_handler()))


def _asyncgen_shutdown_filter(previous: Callable[..., None] | None) -> Callable[..., None]:
    """An asyncio exception handler that drops exactly the close-protocol noise.

    Nothing else: a teardown that fails for a real reason (an OSError closing a
    file, a ValueError out of a `finally`) still reaches the previous handler,
    and so does any other loop error.
    """

    def handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        # asyncio appends the generator's repr to the message, so match the stem.
        if (
            str(context.get("message", "")).startswith(_ASYNCGEN_SHUTDOWN_MESSAGE)
            and isinstance(exc, RuntimeError)
            and any(needle in str(exc) for needle in _ASYNCGEN_PROTOCOL_ERRORS)
        ):
            return
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    return handler


def run_owning_loop(coro: Any) -> Any:
    """``asyncio.run(coro)``, minus one upstream traceback on the way out.

    A completed turn used to print a `RuntimeError` traceback after its own
    telemetry line, because `asyncio.run`'s last act is `shutdown_asyncgens()`
    and the transport leaves a response-body iterator suspended at a yield whose
    teardown awaits. We cannot close that iterator ourselves: it is not the
    object the SDK closes (`PoolByteStream.aclose()` closes `self._stream`, a
    different object), and it belongs to the transport, not to js. The defect is
    upstream, so this narrows what the loop reports rather than pretending to
    fix it — only during OUR shutdown of OUR loop, and only for errors the close
    protocol itself raised.
    """
    runner = asyncio.Runner()
    try:
        return runner.run(coro)
    finally:
        # The model calls' shared HTTP clients die with this loop; close their
        # connections on it. Only a process that made a model call has them.
        model_client = sys.modules.get(f"{__package__}.model_client")
        if model_client is not None:
            with contextlib.suppress(Exception):
                runner.run(model_client.close_shared_http_clients())
        install_asyncgen_shutdown_filter(runner.get_loop())
        runner.close()
