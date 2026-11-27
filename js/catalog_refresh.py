"""`python -m js.catalog_refresh AGE_MAX`: download the models.dev catalog
into the home's cache unless it was checked within AGE_MAX seconds.
`model_metadata.refresh_in_background` starts it; it prints nothing."""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    from . import model_metadata, paths

    paths.configure_home()
    try:
        model_metadata.refresh_if_older(float(argv[0]))
    except Exception:  # noqa: BLE001 - nobody is listening; the next stale check starts another
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
