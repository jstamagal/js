import pytest

from js.sampling import parse_template_kwargs


@pytest.mark.parametrize(("text", "expected"), [
    ('enable_thinking=false,stop=["a","b"]', {"enable_thinking": False, "stop": ["a", "b"]}),
    ('config={"items":[1,2],"inner":{"x":true,"y":null}},mode=fast',
     {"config": {"items": [1, 2], "inner": {"x": True, "y": None}}, "mode": "fast"}),
    ('label="a,b=c",next=1', {"label": "a,b=c", "next": 1}),
    (r'label="a\"b,c\\d",next=2', {"label": 'a"b,c\\d', "next": 2}),
    (' mode = high , count = 2 , enabled = true ', {"mode": "high", "count": 2, "enabled": True}),
    ('value=[],mapping={}', {"value": [], "mapping": {}}),
])
def test_template_kwargs_preserve_json_value_boundaries(text, expected):
    assert parse_template_kwargs(text) == expected


@pytest.mark.parametrize("text", ["x=1,x=2", "x=1,", ",x=1", "x=", "=1", "missing"])
def test_template_kwargs_still_reject_invalid_entries(text):
    with pytest.raises(ValueError):
        parse_template_kwargs(text)
