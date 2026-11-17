"""Model and tool text as text, never as terminal control sequences.

Every byte of model or tool output passes `clean()` before a terminal sees it,
so a model that cats a binary cannot leave the terminal needing `reset`.
"""

from __future__ import annotations

import re

# The states of the DEC/ECMA-48 escape parser (Paul Williams, "A parser for
# DEC's ANSI-compatible video terminals"). Only ground prints.
GROUND, ESCAPE, ESCAPE_INTERMEDIATE = "ground", "escape", "escape_intermediate"
CSI_ENTRY, CSI_PARAM, CSI_INTERMEDIATE, CSI_IGNORE = "csi_entry", "csi_param", "csi_intermediate", "csi_ignore"
DCS_ENTRY, DCS_PARAM, DCS_INTERMEDIATE = "dcs_entry", "dcs_param", "dcs_intermediate"
DCS_PASSTHROUGH, DCS_IGNORE = "dcs_passthrough", "dcs_ignore"
OSC_STRING, SOS_PM_APC_STRING = "osc_string", "sos_pm_apc_string"
_STRINGS = frozenset({DCS_ENTRY, DCS_PARAM, DCS_INTERMEDIATE, DCS_PASSTHROUGH, DCS_IGNORE, OSC_STRING,
                      SOS_PM_APC_STRING})

# Ground: the next byte that is not printed as it is (C0 but newline and tab,
# DEL, C1). A string: the next byte that can end it (BEL, CAN, SUB, ESC, C1).
_GROUND_STOP = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_STRING_STOP = re.compile(r"[\x07\x18\x1a\x1b\x80-\x9f]")
# Ground, for a cleaner that keeps the other controls: ESC and C1 only.
_ESCAPE_STOP = re.compile(r"[\x1b\x80-\x9f]")
_UNPRINTED = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Where ESC and each C1 introducer lead, from any state.
_AFTER_ESC = {"[": CSI_ENTRY, "]": OSC_STRING, "P": DCS_ENTRY, "X": SOS_PM_APC_STRING, "^": SOS_PM_APC_STRING,
              "_": SOS_PM_APC_STRING}
_C1 = {"\x9b": CSI_ENTRY, "\x9d": OSC_STRING, "\x90": DCS_ENTRY, "\x98": SOS_PM_APC_STRING,
       "\x9e": SOS_PM_APC_STRING, "\x9f": SOS_PM_APC_STRING}


def _sequence_step(state: str, char: str) -> str:
    """The state after ``char`` in one of the non-string sequence states.
    A character past 7-bit ASCII ends the sequence."""
    code = ord(char)
    if code >= 0xA0:
        return GROUND
    if code < 0x20 or code == 0x7F:
        return state  # executed or ignored: the sequence goes on
    if state == ESCAPE:
        if char in _AFTER_ESC:
            return _AFTER_ESC[char]
        return ESCAPE_INTERMEDIATE if code <= 0x2F else GROUND
    if state == ESCAPE_INTERMEDIATE:
        return ESCAPE_INTERMEDIATE if code <= 0x2F else GROUND
    if code >= 0x40:
        return GROUND  # the final byte of a CSI
    if state == CSI_IGNORE:
        return CSI_IGNORE
    if code <= 0x2F:
        return CSI_INTERMEDIATE
    if state == CSI_INTERMEDIATE or char == ":" or (state == CSI_PARAM and code >= 0x3C):
        return CSI_IGNORE
    return CSI_PARAM


def _dcs_step(state: str, char: str) -> str:
    """The state after ``char`` inside a DCS, before its passthrough."""
    code = ord(char)
    if code < 0x20 or code == 0x7F or state in (DCS_PASSTHROUGH, DCS_IGNORE):
        return state
    if code >= 0x40:
        return DCS_PASSTHROUGH
    if code <= 0x2F:
        return DCS_INTERMEDIATE
    if state == DCS_INTERMEDIATE or char == ":" or (state == DCS_PARAM and code >= 0x3C):
        return DCS_IGNORE
    return DCS_PARAM


class StreamCleaner:
    """Model and tool text as text, never as terminal control sequences: the
    DEC/ECMA-48 escape parser, kept across chunks. Ground text is passed on
    (newline and tab with it; every other C0 and C1 control dropped); CSI,
    other escapes and the strings (OSC, DCS, SOS, PM, APC, ended by ST or
    BEL) are dropped whole wherever a chunk cuts them. A string still open
    when the text ends is not a control sequence: its text comes back from
    `flush`. Model and tool text is cleaned here, at intake, and `clean` is
    this run over one whole text, so any chunking cleans the same. With
    ``controls``, ground keeps every C0 control but ESC, and DEL: the
    transcript keeps carriage returns as they were shown."""

    def __init__(self, *, controls: bool = False) -> None:
        self.state = GROUND
        self._string: list[str] = []  # the text of a string still open
        self._controls = controls
        self._ground_stop = _ESCAPE_STOP if controls else _GROUND_STOP

    def feed(self, text: str) -> str:
        out: list[str] = []
        pos, size = 0, len(text)
        while pos < size:
            state = self.state
            if state == GROUND:
                stop = self._ground_stop.search(text, pos)
                end = stop.start() if stop else size
                out.append(text[pos:end])
                pos = end
                if stop is None:
                    break
                char = text[pos]
                pos += 1
                if char == "\x1b":
                    self.state = ESCAPE
                elif char in _C1:
                    self._open(_C1[char])
                continue
            if state in _STRINGS:
                stop = _STRING_STOP.search(text, pos)
                end = stop.start() if stop else size
                piece = text[pos:end]
                if state in (DCS_ENTRY, DCS_PARAM, DCS_INTERMEDIATE):
                    for index, char in enumerate(piece):
                        self._string.append(char)
                        state = _dcs_step(state, char)
                        if state == DCS_PASSTHROUGH or state == DCS_IGNORE:
                            self._string.append(piece[index + 1:])
                            break
                    self.state = state
                else:
                    self._string.append(piece)
                pos = end
                if stop is None:
                    break
                char = text[pos]
                pos += 1
                # BEL, ST, CAN or SUB end the string; ESC or another C1 ends it
                # and starts what it starts.
                self._string = []
                if char == "\x1b":
                    self.state = ESCAPE
                elif char in _C1:
                    self._open(_C1[char])
                else:
                    self.state = GROUND
                continue
            char = text[pos]
            pos += 1
            if char == "\x1b":
                self.state = ESCAPE
            elif char in _C1:
                self._open(_C1[char])
            elif char in "\x18\x1a" or 0x80 <= ord(char) <= 0x9F:
                self.state = GROUND
            else:
                following = _sequence_step(state, char)
                if following in _STRINGS:
                    self._open(following)
                elif following == GROUND and ord(char) >= 0xA0:
                    # Not part of a sequence: the sequence ends and it prints.
                    self.state = GROUND
                    out.append(char)
                else:
                    self.state = following
        return "".join(out)

    def _open(self, state: str) -> None:
        self.state, self._string = state, []

    def flush(self) -> str:
        """The text has ended: an escape sequence still open is dropped, and
        the text of a string never ended is passed on."""
        held = "".join(self._string) if self.state in _STRINGS else ""
        self.state, self._string = GROUND, []
        return held if self._controls else _UNPRINTED.sub("", held)


def clean(text: str) -> str:
    """Model and tool output as text, never as terminal control sequences
    (`StreamCleaner` over the whole text)."""
    cleaner = StreamCleaner()
    return cleaner.feed(text) + cleaner.flush()
