"""PARANOID_MODE: how much js hides secrets.

- off: nothing hidden. Keys echo as typed and print in full.
- on: keys are masked where js shows them: login and install prompts do not
  echo, `/set` shows a secret setting as <set>, `js --login` views show a
  key's ends only, compaction logs drop key fields, and search-tool errors
  to the model carry [REDACTED] for the key.
- extra: everything in on, and every model request carries
  ``X-JS-Scrub: on`` so a CLIProxyAPI secret-scrub plugin swaps keys for
  placeholders at the wire.

The ``secret`` flag on a setting names what on and extra hide.
"""

from __future__ import annotations

from typing import Any

from . import settings as _settings

MODES: tuple[str, ...] = ("off", "on", "extra")
SCRUB_HEADER = "X-JS-Scrub"


def read(raw: Any) -> str:
    """A stored PARANOID_MODE value as one of `MODES`; anything else is off."""
    value = str(raw or "").strip().lower()
    return value if value in MODES else "off"


def mode(settings: dict | None = None) -> str:
    """PARANOID_MODE from ``settings``. With no store, from every jsrc layer
    and the environment, as a run started here loads them."""
    if settings is None:
        from .config import run_settings

        settings = run_settings()
    return read(_settings.knob(settings, "PARANOID_MODE"))


def masks(level: str) -> bool:
    """True when ``level`` hides keys from view."""
    return level != "off"


def wire_headers(level: str) -> dict[str, str]:
    """The headers ``level`` adds to every model request."""
    return {SCRUB_HEADER: "on"} if level == "extra" else {}
