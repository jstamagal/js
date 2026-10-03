"""The session picker's pure layer: session kinds, the search grammar, the
filters and order, and the list's views with branches nested."""

from __future__ import annotations

from datetime import datetime

import pytest

from js import paths
from js import session_query as Q

HOME = "/home/op"
CWD = "/home/op/js"
# A fixed clock: 2026-09-29 12:00 local time.
NOW = datetime(2026, 9, 29, 12, 0).timestamp()


def _ts(*parts: int) -> float:
    return datetime(*parts).timestamp()


def _session(path: str, **fields) -> Q.Session:
    base = dict(agent="defaultagent", cwd=CWD, mode="repl", started=_ts(2026, 9, 29, 8, 0),
                last=_ts(2026, 9, 29, 9, 0), turns=5, messages=12, tool_calls=4, replied=True,
                final_len=2000, models=("deepseek-v4-flash",))
    base.update(fields)
    return Q.Session(path=path, **base)


def _parse(text: str) -> Q.Query:
    return Q.parse_query(text, now=NOW, home=HOME, cwd=CWD)


def _paths(sessions: list[Q.Session]) -> list[str]:
    return [session.path for session in sessions]


# --- kinds -------------------------------------------------------------------


def test_a_session_where_nothing_came_back_is_empty():
    assert Q.kind(_session("a", turns=1, messages=1, tool_calls=0, replied=False, final_len=0)) == Q.EMPTY


def test_one_message_few_calls_and_a_short_reply_is_quick():
    assert Q.kind(_session("a", turns=1, tool_calls=2, final_len=999)) == Q.QUICK


@pytest.mark.parametrize("fields", [
    {"turns": 2, "tool_calls": 0, "final_len": 10},
    {"turns": 1, "tool_calls": 3, "final_len": 10},
    {"turns": 1, "tool_calls": 0, "final_len": 1000},
])
def test_more_turns_calls_or_reply_is_not_quick(fields):
    assert Q.kind(_session("a", **fields)) == Q.SHOWN


def test_subagent_and_commit_runs_are_their_own_kinds_whatever_their_size():
    assert Q.kind(_session("a", mode="subagent")) == Q.SUBAGENT
    assert Q.kind(_session("a", mode=None, parent="/p.jsonl")) == Q.SUBAGENT
    assert Q.kind(_session("a", mode="commit", turns=1, replied=False)) == Q.SCRIPT


@pytest.mark.parametrize("stem", ["task-1790000000-ab12", "task-1789647867292-45d82d1580a3c4ed"])
def test_a_task_named_run_without_mode_or_parent_is_a_nameless_subagent(stem):
    # Subagent runs filed before mode and parent were recorded carry neither.
    session = _session(str(paths.sessions_root() / "-home-op" / f"{stem}.jsonl"), mode=None)
    assert Q.kind(session) == Q.SUBAGENT
    assert session.name is None


def test_hidden_kinds_show_only_with_all_or_when_the_query_names_them():
    quick = _session("q", turns=1, tool_calls=0, final_len=5)
    empty = _session("e", replied=False)
    child = _session("s", mode="subagent")
    commit = _session("c", mode="commit")
    normal = _session("n")
    everything = [quick, empty, child, commit, normal]
    assert _paths(Q.select(everything, _parse(""), show_all=False)) == ["n"]
    assert sorted(_paths(Q.select(everything, _parse(""), show_all=True))) == ["c", "e", "n", "q", "s"]
    assert _paths(Q.select(everything, _parse("mode:quick"), show_all=False)) == ["q"]
    assert _paths(Q.select(everything, _parse("mode:commit"), show_all=False)) == ["c"]


# --- order and views ---------------------------------------------------------


def test_the_list_is_newest_first_across_dirs_and_agents():
    old = _session("old", started=_ts(2026, 9, 1, 8, 0), cwd="/srv", agent="research")
    new = _session("new", started=_ts(2026, 9, 29, 9, 0))
    mid = _session("mid", started=_ts(2026, 9, 17, 10, 0), cwd=HOME)
    assert _paths(Q.select([old, new, mid], _parse(""), show_all=False)) == ["new", "mid", "old"]


def test_a_session_written_to_since_comes_before_newer_ones():
    resumed = _session("resumed", started=_ts(2026, 7, 18, 4, 0), last=_ts(2026, 9, 29, 11, 30))
    new = _session("new", started=_ts(2026, 9, 29, 9, 0), last=_ts(2026, 9, 29, 9, 30))
    assert _paths(Q.select([new, resumed], _parse(""), show_all=False)) == ["resumed", "new"]


def test_branches_nest_under_their_parent_in_start_order():
    parent = _session("p", started=_ts(2026, 9, 17, 10, 0))
    first = _session("b1", branch_of="p", branch_point=31, started=_ts(2026, 9, 17, 11, 0))
    second = _session("b2", branch_of="p", branch_point=40, started=_ts(2026, 9, 17, 12, 0))
    nested = _session("b3", branch_of="b1", branch_point=3, started=_ts(2026, 9, 17, 13, 0))
    other = _session("o", started=_ts(2026, 9, 29, 8, 0))
    ordered = Q.select([parent, first, second, nested, other], _parse(""), show_all=False)
    items = Q.build_items(ordered, "flat", home=HOME)
    assert [(item.session.path, item.depth) for item in items] == [
        ("o", 0), ("p", 0), ("b1", 1), ("b3", 2), ("b2", 1)]
    assert [item.last for item in items if item.depth] == [False, True, True]


def test_a_branch_whose_parent_is_not_listed_stands_on_its_own():
    branch = _session("b", branch_of="gone", branch_point=4)
    items = Q.build_items([branch], "flat", home=HOME)
    assert [(item.session.path, item.depth) for item in items] == [("b", 0)]


def test_the_views_group_by_start_dir_or_agent_in_order_of_first_appearance():
    sessions = [
        _session("a", cwd=CWD, agent="defaultagent"),
        _session("b", cwd=HOME, agent="research"),
        _session("c", cwd=CWD, agent="research"),
    ]
    by_dir = Q.build_items(sessions, "dir", home=HOME)
    assert [item.group or item.session.path for item in by_dir] == ["[~/js]", "a", "c", "[~]", "b"]
    by_agent = Q.build_items(sessions, "agent", home=HOME)
    assert [item.group or item.session.path for item in by_agent] == [
        "[defaultagent]", "a", "[research]", "b", "c"]


def test_a_nested_branch_row_names_its_branch_point_and_keeps_the_count_columns():
    parent = _session("p", turns=41)
    branch = _session("b", branch_of="p", branch_point=31, turns=12)
    items = Q.build_items([parent, branch], "flat", home=HOME)
    top = Q.session_line(items[0], now=NOW, home=HOME)
    nested = Q.session_line(items[1], now=NOW, home=HOME)
    assert "#0031" in nested
    assert top.index("41") == nested.index("12")


def test_a_named_session_row_shows_its_name_and_a_generated_one_does_not():
    folder = paths.sessions_root() / "-home-op-js"
    named = _session(str(folder / "modelswap.jsonl"))
    generated = _session(str(folder / "2026-09-29T0800-6d65.jsonl"))
    subagent = _session(str(folder / "modelswap" / "task-1790000000-ab12.jsonl"))

    named_row = Q.session_line(Q.Item(named), now=NOW, home=HOME)
    generated_row = Q.session_line(Q.Item(generated), now=NOW, home=HOME)

    assert "modelswap" in named_row
    assert "modelswap" not in generated_row and "6d65" not in generated_row
    assert (named.name, generated.name, subagent.name) == ("modelswap", None, None)


def test_a_hidden_kind_is_marked_in_the_tags_column():
    assert Q.tags_text(_session("q", turns=1, tool_calls=0, final_len=5)) == Q.QUICK
    assert Q.tags_text(_session("n")) == "-"


# --- the query grammar -------------------------------------------------------


def test_words_and_phrases_become_one_required_fts_term_each():
    query = _parse('niri motherboard "exact phrase"')
    assert query.words == ["niri", "motherboard"]
    assert query.phrases == ["exact phrase"]
    assert Q.fts_expression(query) == '"niri"* AND "motherboard"* AND "exact phrase"'
    assert query.ranked


def test_a_quote_inside_a_word_is_escaped_for_fts():
    assert Q.fts_expression(_parse('it"s')) == '"it""s"*'


def test_no_words_means_no_fts_term_and_newest_first():
    query = _parse("agent:defaultagent >2")
    assert Q.fts_expression(query) is None
    assert not query.ranked


@pytest.mark.parametrize("text,keep", [
    (">10", ["huge", "many"]),
    ("<2", ["one"]),
    (">=10,<=20", ["many"]),
    (">=5,<=5", ["five"]),
])
def test_counts_filter_on_turns(text, keep):
    sessions = [_session("one", turns=1, tool_calls=5), _session("five", turns=5),
                _session("many", turns=15), _session("huge", turns=40)]
    assert sorted(_paths(Q.select(sessions, _parse(text), show_all=False))) == keep


def test_dates_match_a_session_active_in_the_span():
    sessions = [
        _session("today", started=_ts(2026, 9, 29, 8, 0), last=_ts(2026, 9, 29, 9, 0)),
        _session("yesterday", started=_ts(2026, 9, 28, 8, 0), last=_ts(2026, 9, 28, 9, 0)),
        _session("across", started=_ts(2026, 9, 28, 23, 0), last=_ts(2026, 9, 29, 1, 0)),
        _session("august", started=_ts(2026, 8, 10, 8, 0), last=_ts(2026, 8, 10, 9, 0)),
        _session("lastyear", started=_ts(2025, 12, 31, 8, 0), last=_ts(2025, 12, 31, 9, 0)),
    ]

    def pick(text):
        return sorted(_paths(Q.select(sessions, _parse(text), show_all=False)))

    assert pick("today") == ["across", "today"]
    assert pick("yesterday") == ["across", "yesterday"]
    assert pick("week") == ["across", "today", "yesterday"]
    assert pick("2026") == ["across", "august", "today", "yesterday"]
    assert pick("2025") == ["lastyear"]
    assert pick("2026-08") == ["august"]
    assert pick("2026-09-28") == ["across", "yesterday"]


def test_a_date_prefix_is_the_date_plus_the_rest():
    query = _parse("today:niri")
    assert [label for label, _, _ in query.dates] == ["today"]
    assert query.words == ["niri"]


def test_agent_takes_a_glob():
    sessions = [_session("d", agent="defaultagent"), _session("r", agent="deep-research-2"),
                _session("n", agent=None)]
    assert _paths(Q.select(sessions, _parse("agent:defaultagent"), show_all=False)) == ["d"]
    assert _paths(Q.select(sessions, _parse("agent:*research*"), show_all=False)) == ["r"]
    assert _paths(Q.select(sessions, _parse("agent:default"), show_all=False)) == []


@pytest.mark.parametrize("pattern,keep", [
    ("dir:~/js", ["js"]),
    ("dir:~/js/*", ["jsjs", "toolkit", "worktree"]),
    ("dir:~/js/**", ["deep", "js", "jsjs", "toolkit", "worktree"]),
    ("dir:~/js/*/toolkit", ["deep"]),
    ("dir:~", ["home"]),
    ("dir:.", ["js"]),
    ("dir:js/", ["jsjs"]),
])
def test_dir_is_a_shell_glob_over_the_start_directory(pattern, keep):
    sessions = [
        _session("home", cwd=HOME),
        _session("js", cwd=f"{HOME}/js"),
        _session("toolkit", cwd=f"{HOME}/js/toolkit"),
        _session("worktree", cwd=f"{HOME}/js/.claude"),
        _session("deep", cwd=f"{HOME}/js/js/toolkit"),
        _session("jsjs", cwd=f"{HOME}/js/js"),
        _session("other", cwd=f"{HOME}/jsother"),
    ]
    assert sorted(_paths(Q.select(sessions, _parse(pattern), show_all=False))) == keep


def test_mode_matches_how_it_started():
    sessions = [_session("p", mode="-p"), _session("r", mode="repl"), _session("pipe", mode="pipe")]
    assert _paths(Q.select(sessions, _parse("mode:-p"), show_all=False)) == ["p"]
    assert _paths(Q.select(sessions, _parse("mode:repl"), show_all=False)) == ["r"]


def test_model_matches_any_stamped_model():
    sessions = [_session("q", models=("deepseek-v4-flash", "qwen/qwen3-coder")),
                _session("d", models=("deepseek-v4-flash",))]
    assert _paths(Q.select(sessions, _parse("model:*qwen*"), show_all=False)) == ["q"]
    assert sorted(_paths(Q.select(sessions, _parse("model:deepseek-v4-flash"), show_all=False))) == ["d", "q"]


def test_tag_matches_a_tag_containing_it():
    sessions = [_session("n", tags=("nfs / mounts", "linux admin")), _session("j", tags=("js",)),
                _session("none")]
    assert _paths(Q.select(sessions, _parse("tag:nfs"), show_all=False)) == ["n"]
    assert _paths(Q.select(sessions, _parse("tag:j*"), show_all=False)) == ["j"]


def test_terms_combine_with_and():
    sessions = [_session("a", agent="defaultagent", turns=20), _session("b", agent="defaultagent", turns=2),
                _session("c", agent="research", turns=20)]
    assert _paths(Q.select(sessions, _parse("agent:defaultagent >10"), show_all=False)) == ["a"]


def test_words_keep_only_scored_sessions_best_score_first():
    sessions = [_session("a"), _session("b"), _session("c")]
    ordered = Q.select(sessions, _parse("niri"), show_all=False, scores={"c": -3.0, "a": -1.0})
    assert _paths(ordered) == ["c", "a"]


def test_the_parse_line_names_every_term():
    described = Q.describe(_parse("niri >10 today agent:x* dir:~/js/** mode:-p model:*qwen* tag:nfs"))
    for part in ("niri", ">10", "today", "x*", "~/js", "-p", "*qwen*", "nfs"):
        assert part in described
    assert Q.describe(_parse("")) != Q.describe(_parse("niri"))


def test_an_excerpt_starts_near_the_first_match_and_moves_its_spans():
    text = "x" * 500 + " niri here"
    start = text.index("niri")
    shown, spans = Q.excerpt(text, ((start, start + 4),), width=80)
    assert len(spans) == 1
    assert shown[spans[0][0]:spans[0][1]] == "niri"
    assert len(shown) <= 82
