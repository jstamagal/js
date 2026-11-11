import pytest

from js.toolkit.wiki.helpers import slugify


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("a" * 59 + " bbb", "a" * 59),
        ("a" * 60 + " bbb", "a" * 60),
        ("a" * 58 + " bbb", "a" * 58 + "-b"),
        ("!!! " + "a" * 70, "a" * 60),
        ("!!!", "untitled"),
        ("", "untitled"),
    ],
)
def test_slugify_has_no_edge_separator_after_truncation(title, expected):
    result = slugify(title)
    assert result == expected
    assert 0 < len(result) <= 60
    assert not result.startswith("-")
    assert not result.endswith("-")
