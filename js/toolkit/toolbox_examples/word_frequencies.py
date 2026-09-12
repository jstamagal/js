# js-toolbox: {"history":[{"date":"2026-09-12","model":"js","note":"shipped example","revision":1}],"name":"word_frequencies","revision":1}

"""The example tool that ships with js: self-contained, no session state."""

import collections
import re

_WORD = re.compile(r"[a-z0-9']+")


def word_frequencies(text, limit=10):
    """Return the `limit` most common words in `text`, most frequent first."""
    counts = collections.Counter(_WORD.findall(text.lower()))
    return counts.most_common(limit)
