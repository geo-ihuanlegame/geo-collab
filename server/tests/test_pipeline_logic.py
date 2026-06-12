import pytest

from server.app.modules.pipelines.flow_meta import apply_input_mapping, should_skip
from server.app.modules.pipelines.nodes import base as node_base
from server.app.modules.pipelines.nodes.base import NodeRunContext
from server.app.modules.pipelines.nodes.input_node import run_input
from server.app.modules.pipelines.snapshot import nodes_to_snapshot, snapshot_to_node_dicts
from server.app.shared.errors import ValidationError


def test_apply_input_mapping_copies_upstream_to_target_names():
    meta = {"inputMapping": [{"from": "title", "to": "question_text"}]}
    out = apply_input_mapping(meta, {"title": "Hello"})
    assert out == {"question_text": "Hello"}


def test_apply_input_mapping_passes_through_when_no_mapping():
    # 无 inputMapping（meta 为 None / 无该键 / 空列表）→ 默认透传整个上游：
    # 选定上游节点（或默认合并全部上游）即自动把上游字段传给本节点，无需手填同名映射。
    assert apply_input_mapping(None, {"a": "b"}) == {"a": "b"}
    assert apply_input_mapping({}, {"a": "b"}) == {"a": "b"}
    assert apply_input_mapping({"inputMapping": []}, {"question_text": "Q"}) == {
        "question_text": "Q"
    }


def test_apply_input_mapping_explicit_mapping_filters_unmapped():
    # 配了映射 → 只注入命中的字段（改名/筛选），未映射的字段不透传。
    meta = {"inputMapping": [{"from": "title", "to": "question_text"}]}
    assert apply_input_mapping(meta, {"title": "Hi", "other": "x"}) == {"question_text": "Hi"}


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


class _FakeNode:
    def __init__(self, node_type, name, node_index, config, flow_meta):
        self.node_type, self.name, self.node_index = node_type, name, node_index
        self.config, self.flow_meta = config, flow_meta


def test_snapshot_round_trip_preserves_order_and_fields():
    nodes = [
        _FakeNode("input", "源", 0, {"question_text": "Q"}, None),
        _FakeNode(
            "ai_generate",
            "生文",
            1,
            {"prompt_template_id": 5, "count": 2},
            {
                "schemaVersion": 1,
                "inputMapping": [{"from": "question_text", "to": "question_text"}],
            },
        ),
    ]
    snap = nodes_to_snapshot(nodes)
    assert snap["schemaVersion"] == 1
    assert [n["node_index"] for n in snap["nodes"]] == [0, 1]

    dicts = snapshot_to_node_dicts(snap)
    assert dicts[0]["node_type"] == "input"
    assert dicts[1]["config"]["count"] == 2
    assert dicts[1]["flow_meta"]["inputMapping"][0]["to"] == "question_text"


def test_snapshot_to_node_dicts_handles_empty():
    assert snapshot_to_node_dicts(None) == []
    assert snapshot_to_node_dicts({}) == []


def test_registry_register_and_get():
    node_base.register("dummy", lambda ctx: node_base.NodeResult(output={"ok": 1}, article_ids=[]))
    handler = node_base.get_handler("dummy")
    res = handler(None)
    assert res.output == {"ok": 1}


def test_registry_unknown_type_raises():
    with pytest.raises(ValidationError):
        node_base.get_handler("nope-does-not-exist")


def test_input_node_outputs_question_text():
    ctx = NodeRunContext(
        session_factory=None,
        user_id=1,
        config={"question_text": "今天写什么"},
        inputs={},
        upstream={},
    )
    res = run_input(ctx)
    assert res.output == {"question_text": "今天写什么"}
    assert res.article_ids == []


def test_ai_generate_rejects_excessive_count():
    import pytest

    from server.app.modules.pipelines.nodes.ai_generate_node import run_ai_generate
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.shared.errors import ValidationError

    ctx = NodeRunContext(
        session_factory=lambda: None,
        user_id=1,
        config={"prompt_template_id": 1, "count": 9999, "question_text": "x"},
        inputs={},
        upstream={},
    )
    with pytest.raises(ValidationError):
        run_ai_generate(ctx)
