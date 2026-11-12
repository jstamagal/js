"""Linked image destinations and their enclosing links are both actionable."""

import pytest

from js.toolkit.search import _absolutize


@pytest.mark.parametrize("source, expected", [
    ("[![logo](/img/logo.png)](/home)",
     "[![logo](https://example.test/img/logo.png)](https://example.test/home)"),
    ('[![logo](img.png "image title")](page "link title")',
     '[![logo](https://example.test/dir/img.png "image title")](https://example.test/dir/page "link title")'),
    ("[![logo](/img.png)](/go?to=https://other.test)",
     "[![logo](https://example.test/img.png)](https://example.test/go?to=https://other.test)"),
    ("[![logo](https://cdn.test/img.png)](#top)",
     "[![logo](https://cdn.test/img.png)](#top)"),
    ("[![logo](/img.png)](http://[invalid)",
     "[![logo](https://example.test/img.png)](http://[invalid)"),
    ("[![logo](/img.png)](//other.test/home)",
     "[![logo](https://example.test/img.png)](https://other.test/home)"),
    ("[![logo](/img.png)](/home", "[![logo](https://example.test/img.png)](/home"),
    ('[plain](/home "title") ![logo](/img.png)',
     '[plain](https://example.test/home "title") ![logo](https://example.test/img.png)'),
])
def test_browse_rewrites_linked_images(source, expected):
    assert _absolutize(source, "https://example.test/dir/page") == expected
