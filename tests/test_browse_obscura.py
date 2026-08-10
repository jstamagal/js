"""browse() against the real obscura binary.

These hit the live network on purpose: obscura is a browser, and the defects
being pinned here (relative hrefs, which dumps the binary actually supports,
screenshot-versus-dump exclusivity) are all properties of the real binary that
a fake subprocess would let us assert wrongly.
"""
from __future__ import annotations

import shutil

import pytest

from js.toolkit import ToolContext
from js.toolkit.search import _absolutize, browse


requires_obscura = pytest.mark.skipif(
    shutil.which("obscura") is None, reason="obscura binary not installed"
)

PAGE = "https://quotes.toscrape.com/js/"


def test_relative_hrefs_are_rewritten_against_the_page_url():
    out = _absolutize("[Login](/login) [deep](a/b)", PAGE)
    assert "(https://quotes.toscrape.com/login)" in out
    assert "(https://quotes.toscrape.com/js/a/b)" in out


def test_in_page_anchors_and_absolute_targets_are_left_alone():
    raw = "[top](#top) [abs](https://x.test/c) [mail](mailto:a@b.c) [tel](tel:+1)"
    assert _absolutize(raw, PAGE) == raw


def test_image_targets_are_rewritten_too():
    assert _absolutize("![logo](/i.png)", PAGE) == "![logo](https://quotes.toscrape.com/i.png)"


@requires_obscura
def test_markdown_dump_returns_fetchable_links(tmp_path):
    context = ToolContext(cwd=tmp_path)
    out = browse(PAGE, context=context)
    assert not out.startswith("ERROR:"), out
    assert "(https://quotes.toscrape.com/login)" in out
    assert "](/login)" not in out


@requires_obscura
@pytest.mark.parametrize("dump", ["original", "assets", "cookies"])
def test_obscura_only_dumps_are_accepted(dump, tmp_path):
    context = ToolContext(cwd=tmp_path)
    out = browse(PAGE, dump=dump, context=context)
    assert not out.startswith("ERROR:"), out


def test_an_unsupported_dump_names_every_supported_one(tmp_path):
    context = ToolContext(cwd=tmp_path)
    out = browse("https://example.com", dump="pdf", context=context)
    assert out.startswith("ERROR:")
    for name in ("markdown", "text", "html", "links", "original", "assets", "cookies"):
        assert name in out


@requires_obscura
def test_screenshot_writes_a_png_and_says_the_dump_is_unavailable(tmp_path):
    context = ToolContext(cwd=tmp_path)
    out = browse("https://example.com", screenshot="shot.png", context=context)
    assert not out.startswith("ERROR:"), out
    shot = tmp_path / "shot.png"
    assert shot.exists()
    assert shot.read_bytes()[:4] == b"\x89PNG"
    assert str(shot) in out
    # obscura emits either a picture or a dump, never both; the result has to say
    # so, or an empty body reads to the model as "this page has no content".
    assert "never both" in out


@requires_obscura
def test_a_screenshot_path_is_written_whatever_it_is_called(tmp_path):
    """obscura always writes PNG bytes, and js identifies images by magic bytes.
    Rejecting a non-.png name would cost the model a turn for nothing."""
    context = ToolContext(cwd=tmp_path)
    out = browse("https://example.com", screenshot="shot.jpg", context=context)
    assert not out.startswith("ERROR:"), out
    shot = tmp_path / "shot.jpg"
    assert shot.exists()
    assert shot.read_bytes()[:4] == b"\x89PNG"


@requires_obscura
def test_a_screenshot_into_a_missing_directory_creates_it(tmp_path):
    context = ToolContext(cwd=tmp_path)
    out = browse("https://example.com", screenshot="shots/nested/page.png", context=context)
    assert not out.startswith("ERROR:"), out
    assert (tmp_path / "shots" / "nested" / "page.png").exists()


def test_browse_uses_the_browser_budget_not_the_plain_fetch_one(tmp_path, monkeypatch):
    """browse drives a browser engine; 15s is a plain-HTTP number. Pin that the
    process budget comes from browse_timeout_s and that obscura is told to give
    up one second earlier, so its graceful path runs before the hard kill."""
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["timeout"] = kwargs.get("timeout")
        raise RuntimeError("stop here")

    monkeypatch.setattr("js.toolkit.search._run_capped", fake_run)
    context = ToolContext(cwd=tmp_path, fetch_timeout_s=15, browse_timeout_s=90)
    browse("https://example.com", context=context)

    assert seen["timeout"] == 90
    argv = seen["argv"]
    assert "--timeout" in argv
    assert argv[argv.index("--timeout") + 1] == "89"
