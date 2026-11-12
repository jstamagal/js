"""Browse links use their URL structure, not URL text inside a query."""

import pytest

from js.toolkit.search import _absolutize


@pytest.mark.parametrize("target, expected", [
    ("/go?to=https://x.com", "https://example.test/go?to=https://x.com"),
    ("page?next=http://x.com", "https://example.test/dir/page?next=http://x.com"),
    ("?next=https://x.com", "https://example.test/dir/current?next=https://x.com"),
    ("../go?to=https://x.com#part", "https://example.test/go?to=https://x.com#part"),
    ("https://other.test/path", "https://other.test/path"),
    ("HTTP://other.test/path", "HTTP://other.test/path"),
    ("http://[invalid", "http://[invalid"),
    ("custom+scheme.resource:payload", "custom+scheme.resource:payload"),
    ("//cdn.test/image", "https://cdn.test/image"),
    ("#section", "#section"),
    ("mailto:a@example.test", "mailto:a@example.test"),
    ("tel:+123", "tel:+123"),
    ("data:text/plain,hello", "data:text/plain,hello"),
    ("custom:resource", "custom:resource"),
])
def test_link_targets_resolve_structurally(target, expected):
    markdown = f'[link]({target} "title") ![image]({target})'
    assert _absolutize(markdown, "https://example.test/dir/current") == (
        f'[link]({expected} "title") ![image]({expected})'
    )
