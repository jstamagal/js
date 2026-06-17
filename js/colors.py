"""ANSI escapes — deterministic UI spec, not arbitrary styling.

Network-related messages from the core system js module use ANSI 16 light yellow.
"""

CYAN     = "\033[96m"  # banner, status accents
MAGENTA  = "\033[95m"  # tool-call trace
YELLOW   = "\033[93m"  # input prompt
WHITE    = "\033[97m"  # assistant text (high contrast on dark term)
GREEN    = "\033[92m"  # banner footer, ok markers
ORANGE   = "\033[91m"  # errors (bright red reads orange)
GREY     = "\033[90m"  # dim metadata (resume markers, truncated notes)
RESET    = "\033[0m"

BOLD     = "\033[1m"
DIM      = "\033[2m"

# Bright ("light") ANSI-16 — the lighter 8. Use these for debug/trace coloring.
BR_BLACK   = "\033[90m"  # grey
BR_RED     = "\033[91m"
BR_GREEN   = "\033[92m"
BR_YELLOW  = "\033[93m"
BR_BLUE    = "\033[94m"
BR_MAGENTA = "\033[95m"
BR_CYAN    = "\033[96m"
BR_WHITE   = "\033[97m"
