from server.app.modules.pipelines.flow_meta import apply_input_mapping, should_skip


def test_apply_input_mapping_copies_upstream_to_target_names():
    meta = {"inputMapping": [{"from": "title", "to": "question_text"}]}
    out = apply_input_mapping(meta, {"title": "Hello"})
    assert out == {"question_text": "Hello"}


def test_apply_input_mapping_none_meta_returns_empty():
    assert apply_input_mapping(None, {"a": "b"}) == {}
    assert apply_input_mapping({}, {"a": "b"}) == {}


def test_should_skip_eq_met_false_not_met_true():
    meta = {"condition": {"field": "status", "op": "eq", "value": "ok"}}
    assert should_skip(meta, {"status": "ok"}) is False
    assert should_skip(meta, {"status": "bad"}) is True


def test_should_skip_no_condition_false():
    assert should_skip({}, {}) is False
    assert should_skip(None, {}) is False


def test_should_skip_neq_and_contains():
    meta = {"condition": {"field": "tags", "op": "contains", "value": "news"}}
    assert should_skip(meta, {"tags": "hot,news"}) is False
    assert should_skip(meta, {"tags": "hot"}) is True
    meta = {"condition": {"field": "tags", "op": "neq", "value": "x"}}
    assert should_skip(meta, {"tags": "y"}) is False
    assert should_skip(meta, {"tags": "x"}) is True


def test_apply_input_mapping_none_upstream_returns_empty():
    assert apply_input_mapping({"inputMapping": [{"from": "a", "to": "b"}]}, None) == {}


def test_should_skip_unknown_op_treated_as_eq():
    meta = {"condition": {"field": "x", "op": "gt", "value": "1"}}
    assert should_skip(meta, {"x": "1"}) is False
    assert should_skip(meta, {"x": "2"}) is True
