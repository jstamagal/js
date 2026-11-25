"""Failure summaries retain stderr's final line, including streamed fragments."""

from io import StringIO

from js.headless import LastLine


def test_unterminated_final_line_is_available_before_flush():
    target = StringIO()
    stream = LastLine(target)
    stream.write("old\nfinal error")

    assert stream.last_line == "final error"
    assert target.getvalue() == "old\nfinal error"


def test_reading_and_flushing_preserve_pending_fragments():
    target = StringIO()
    stream = LastLine(target)
    assert stream.write("old\nfinal ") == len("old\nfinal ")
    assert stream.last_line == "final"
    stream.flush()
    stream.flush()
    stream.write("error")
    assert stream.last_line == "final error"
    stream.write("\nnext")
    assert stream.last_line == "next"
    assert target.getvalue() == "old\nfinal error\nnext"


def test_blank_pending_line_keeps_last_nonblank_completed_line():
    stream = LastLine(StringIO())
    assert stream.last_line == ""
    stream.write(" \t")
    assert stream.last_line == ""
    stream.write("\n  prior error  \n\n\t ")
    assert stream.last_line == "prior error"
    stream.flush()
    assert stream.last_line == "prior error"
