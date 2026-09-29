"""A streamed answer on the async screen reads, on the terminal, as its source.

prompt_toolkit repaints the screen by diff: it writes only the cells that
changed since the last frame and moves the cursor over the rest. The terminal
therefore holds the right text only when every cell is where prompt_toolkit's
own screen model says it is. The terminal model here draws a character
followed by U+FE0F two columns wide, as terminals that honour the emoji
variation selector do.
"""

from __future__ import annotations

import asyncio
import io
import re

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from wcwidth import wcwidth

from js import display, screen

# The answer whose last block showed "reseat607:00scaed if width stays
# x4.glips smack" on yoda, 2026-09-29 (session
# 20260929T101208214271Z-acc53bcfc3ea7213).
ANSWER = """Log itself restricted (`dmesg_restrict=1`) — pulled via `journalctl -k`. Here's what kernel says about them:

**Discrete pair (03:00.0 + 07:00.0, both `1002:73a1`, SR-IOV, 12 VFs, 232W cap):**
- Init **clean**. No page faults, no GPU resets, no ring hangs, no RAS errors since boot (Sep 27 13:04).
- ⚠️ `VF BAR 0/2/5: can't assign; no space / failed to assign` — 6 lines per card. Kernel trying to place huge SR-IOV VF BARs and failing. **Benign if KING not passing VFs to VMs** — noise. If SR-IOV planned, this needs BAR space rework in firmware/resizable BAR.
- ⚠️ **PCIe width look thin**: `03:00` negotiated **x8** (126 Gb/s), `07:00` negotiated **x4** (63 Gb/s), both at 16 GT/s on CPU root ports 00:01.1 / 00:01.3, both "capable of x16". x4 on a GPU = suspect riser seating, slot bifurcation, or dirty contact. Worth `lspci -vv` LnkSta look.

**iGPU (7d:00, Raphael, 2 CU, 1G VRAM):**
- One real line: `[drm] REG_WAIT timeout 1us * 100000 tries - optc31_disable_crtc line:143` — known amdgpu boot quirk disabling CRTC during fb handoff. Harmless, nothing since.

**Tuning visible on cmdline:** `amdgpu.ppfeaturemask=0xffff7fff gpu_recovery=1 mcbp=0 ras_enable=0 ignore_min_pcap=1` — recovery on, RAS off, power cap floor ignored. So RAS reports *wouldn't* appear even if hardware had them.

**Not GPU, saw anyway:** zsh segfaulted Sep 28 02:03 (CPU 11), and `sdc` dropped with `Synchronize Cache(10) failed` Sep 28 18:33 — USB disk yanked or dying.

📌🔨🦍 GPUs clean in dmesg, KING — but that x4 link on 07:00 stinks like unseated card. 🦍 recommend: next round run `lspci -vv` LnkSta check on both, reseat 07:00 card if width stays x4. *lips smack* 🦍💨
🦍💪🤝 APES STRONK TOGETHER"""

_CSI = re.compile(r"\x1b\[([?0-9;]*)[ -/]*([@-~])")
_OSC = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
_SGR = re.compile(r"\x1b\[[0-9;]*m")
_VS16 = "\ufe0f"


class Terminal:
    """The cells a VT100 terminal holds after the bytes prompt_toolkit wrote.

    Autowrap is off (prompt_toolkit turns it off). A wide character fills its
    cell and marks the next one "" as its right half."""

    def __init__(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        self.cells = [[" "] * cols for _ in range(rows)]
        self.x = self.y = 0

    def _put(self, x: int, text: str) -> None:
        row = self.cells[self.y]
        if row[x] == "" and x > 0:
            row[x - 1] = " "
        if x + 1 < self.cols and row[x + 1] == "":
            row[x + 1] = " "
        row[x] = text

    def _draw(self, char: str) -> None:
        width = wcwidth(char)
        if width == 0:
            row = self.cells[self.y]
            left = self.x - 1
            while left > 0 and row[left] == "":
                left -= 1
            if left < 0:
                return
            row[left] += char
            # Emoji presentation: a one-column character becomes two wide.
            if char == _VS16 and wcwidth(row[left][0]) == 1 and self.x < self.cols:
                self._put(self.x, "")
                self.x += 1
            return
        x = min(self.x, self.cols - width)
        self._put(x, char)
        if width == 2:
            self._put(x + 1, "")
        self.x = min(x + width, self.cols - 1)

    def _csi(self, params: str, final: str) -> None:
        if params.startswith("?"):
            return
        numbers = [int(p) if p else 0 for p in params.split(";")] if params else []
        count = (numbers[0] if numbers else 0) or 1
        if final == "H":
            self.y = (numbers[0] if numbers else 1) - 1
            self.x = (numbers[1] if len(numbers) > 1 else 1) - 1
        elif final == "A":
            self.y = max(0, self.y - count)
        elif final == "B":
            self.y = min(self.rows - 1, self.y + count)
        elif final == "C":
            self.x = min(self.cols - 1, self.x + count)
        elif final == "D":
            self.x = max(0, self.x - count)
        elif final == "K":
            self.cells[self.y][self.x:] = [" "] * (self.cols - self.x)
        elif final == "J":
            if numbers and numbers[0] == 2:
                self.cells = [[" "] * self.cols for _ in range(self.rows)]
                return
            self.cells[self.y][self.x:] = [" "] * (self.cols - self.x)
            for y in range(self.y + 1, self.rows):
                self.cells[y] = [" "] * self.cols

    def feed(self, data: str) -> None:
        i = 0
        while i < len(data):
            char = data[i]
            if char == "\x1b":
                match = _CSI.match(data, i) or _OSC.match(data, i)
                if match is not None and match.re is _CSI:
                    self._csi(match.group(1), match.group(2))
                i = match.end() if match is not None else i + 2
                continue
            i += 1
            if char == "\r":
                self.x = 0
            elif char == "\n":
                if self.y == self.rows - 1:
                    self.cells = self.cells[1:] + [[" "] * self.cols]
                else:
                    self.y += 1
            elif char == "\b":
                self.x = max(0, self.x - 1)
            else:
                self._draw(char)

    def lines(self) -> list[str]:
        return ["".join(row).replace(_VS16, "").rstrip() for row in self.cells]


class _Loop:
    """ScreenLive's loop: every scheduled call runs at once, in order."""

    def call_soon_threadsafe(self, fn, *args) -> None:
        fn(*args)


async def _stream_to_terminal(pieces: list[str], rows: int, cols: int) -> tuple[list[str], int]:
    """Stream `pieces` through a Display on the async screen, painting one frame
    after each piece and one after finish(). The terminal's lines and the
    Markdown width the Display rendered at."""
    written = io.StringIO()
    output = Vt100_Output(written, lambda: Size(rows=rows, columns=cols), term="xterm-256color")
    terminal = Terminal(rows, cols)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        app, scrollback = screen.build_app(
            prompt="> ", history=InMemoryHistory(), completer=None,
            on_line=None, on_interrupt=lambda: None, on_eof=lambda: None,
        )

        def paint() -> None:
            with set_app(app):
                app.renderer.render(app, app.layout)
            terminal.feed(written.getvalue())
            written.seek(0)
            written.truncate()

        for n in range(rows + 10):
            scrollback.append(f"earlier line {n}\n")
        scrollback.flush()
        paint()
        live = screen.ScreenLive(_Loop(), scrollback, app)
        sink = display.Display(lambda _text: None, live=live, refresh_s=0)
        for piece in pieces:
            sink.chunk("text", piece)
            paint()
        sink.finish()
        paint()
    return terminal.lines(), live.width()


def _yoda_burst() -> list[str]:
    # One frame while the VF BAR bullet's second line streams; the rest of the
    # answer arrives before the next frame.
    cut = ANSWER.index("SR-IOV VF BARs")
    return [ANSWER[:cut], ANSWER[cut:]]


def _chunks(size: int) -> list[str]:
    return [ANSWER[i:i + size] for i in range(0, len(ANSWER), size)]


@pytest.mark.parametrize(
    ("pieces", "rows", "cols"),
    [
        pytest.param(_yoda_burst(), 46, 100, id="yoda-burst-100-columns"),
        pytest.param(_chunks(12), 46, 92, id="chunks-92-columns"),
    ],
)
def test_streamed_answer_on_the_terminal_reads_as_its_source(pieces, rows, cols):
    shown, width = asyncio.run(_stream_to_terminal(pieces, rows, cols))

    rendered = _SGR.sub("", display.render_markdown(ANSWER, width)).replace(_VS16, "")
    expected = [line.rstrip() for line in rendered.rstrip("\n").split("\n")]
    top = shown.index(expected[0])
    assert shown[top:top + len(expected)] == expected
