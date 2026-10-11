"""ANSI escapes — deterministic UI spec, not arbitrary styling.

Network-related messages from the core system js module use ANSI 16 light yellow
(``BR_YELLOW``, the same escape WARN severity renders with).
"""

CYAN     = "\033[96m"  # startup banner ("me — js agent")
MAGENTA  = "\033[95m"  # startup banner labels
YELLOW   = "\033[93m"  # input prompt
GREEN    = "\033[92m"  # startup banner footer
RESET    = "\033[0m"

GREY     = "\033[90m"  # reasoning text, assistant mark ❮ (dim)

BOLD     = "\033[1m"

# Bright ("light") ANSI-16 — severity and network coloring.
BR_RED     = "\033[91m"
BR_YELLOW  = "\033[93m"