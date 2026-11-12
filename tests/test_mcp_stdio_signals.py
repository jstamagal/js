from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "posix", reason="terminal process-group signals require POSIX")
def test_terminal_interrupt_preserves_stdio_server_and_close_reaps_it():
    fixture = Path(__file__).parent / "fixtures" / "fake_mcp_stdio_server.py"
    # Isolate the driver too: its simulated terminal Ctrl-C must not reach pytest.
    driver = textwrap.dedent("""
        import asyncio
        import os
        import signal
        import sys
        from js.mcp.client import MCPClient
        from js.mcp.transports import StdioTransport

        async def main():
            interrupted = asyncio.Event()
            asyncio.get_running_loop().add_signal_handler(signal.SIGINT, interrupted.set)
            transport = StdioTransport(sys.executable, (sys.argv[1],))
            client = MCPClient(lambda: transport, reconnect_attempts=0)
            try:
                await client.initialize(timeout=2)
                process = transport.process
                assert process is not None
                os.killpg(os.getpgrp(), signal.SIGINT)
                await asyncio.wait_for(interrupted.wait(), 2)
                tools = await client.list_tools(timeout=2)
                assert [tool['name'] for tool in tools] == ['alpha', 'beta']
                assert os.getsid(process.pid) == process.pid
                assert os.getsid(process.pid) != os.getsid(0)
            finally:
                await client.close()
            assert process.returncode == 0

        asyncio.run(main())
    """)
    result = subprocess.run(
        [sys.executable, "-c", driver, str(fixture)],
        start_new_session=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
