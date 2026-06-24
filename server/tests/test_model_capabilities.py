"""ai_compose「模型能力」开关 → litellm 参数映射的单元测试（纯逻辑，无需 DB）。"""

import logging

from server.app.modules.ai_generation.model_capabilities import (
    _provider_of,
    completion_with_capabilities,
)

LOG = logging.getLogger("test")


# ── 假 litellm 响应对象 ───────────────────────────────────────────────────────
class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, id, name, arguments):
        self.id = id
        self.function = _Fn(name, arguments)


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Resp:
    def __init__(self, msg):
        self.choices = [type("C", (), {"message": msg})()]


def _capture(final="ok"):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return _Resp(_Msg(content=final))

    fake.calls = calls
    return fake


def test_provider_detection():
    assert _provider_of("moonshot/kimi-k2.5") == "moonshot"
    assert _provider_of("kimi-thinking") == "moonshot"
    assert _provider_of("anthropic/claude-opus-4-8") == "anthropic"
    assert _provider_of("openai/gpt-4o") == "openai"
    assert _provider_of("gpt-4o") == "openai"
    assert _provider_of("gemini/gemini-2.0") == "gemini"
    assert _provider_of("deepseek/deepseek-v4-flash") == "other"


def test_deep_thinking_injects_reasoning_effort_and_drops_anthropic_temperature():
    fake = _capture()
    base = {
        "model": "anthropic/claude-opus-4-8",
        "messages": [{"role": "user", "content": "q"}],
        "temperature": 0,
    }
    completion_with_capabilities(
        completion=fake,
        base_kwargs=base,
        model="anthropic/claude-opus-4-8",
        web_search=False,
        deep_thinking=True,
        logger=LOG,
    )
    kw = fake.calls[0]
    assert kw["reasoning_effort"] == "high"
    assert kw["drop_params"] is True
    assert "temperature" not in kw  # Anthropic 扩展思考与 temperature=0 冲突 → 去掉


def test_deep_thinking_keeps_temperature_for_non_anthropic():
    fake = _capture()
    base = {"model": "openai/gpt-4o", "messages": [], "temperature": 0}
    completion_with_capabilities(
        completion=fake,
        base_kwargs=base,
        model="openai/gpt-4o",
        web_search=False,
        deep_thinking=True,
        logger=LOG,
    )
    assert fake.calls[0]["reasoning_effort"] == "high"
    assert fake.calls[0]["temperature"] == 0


def test_web_search_native_for_anthropic_openai():
    for model in ("anthropic/claude-opus-4-8", "openai/gpt-4o"):
        fake = _capture()
        completion_with_capabilities(
            completion=fake,
            base_kwargs={"model": model, "messages": []},
            model=model,
            web_search=True,
            deep_thinking=False,
            logger=LOG,
        )
        assert fake.calls[0]["web_search_options"] == {}


def test_web_search_noop_for_unsupported_provider():
    fake = _capture()
    completion_with_capabilities(
        completion=fake,
        base_kwargs={"model": "deepseek/deepseek-v4-flash", "messages": []},
        model="deepseek/deepseek-v4-flash",
        web_search=True,
        deep_thinking=False,
        logger=LOG,
    )
    kw = fake.calls[0]
    assert "web_search_options" not in kw
    assert "tools" not in kw
    assert kw["drop_params"] is True  # 靠 drop_params 静默忽略


def test_moonshot_web_search_tool_loop():
    seq = [
        _Resp(_Msg(content=None, tool_calls=[_ToolCall("c1", "$web_search", '{"q":"x"}')])),
        _Resp(_Msg(content="final")),
    ]
    state = {"i": 0, "kwargs": []}

    def fake(**kwargs):
        state["kwargs"].append(kwargs)
        r = seq[state["i"]]
        state["i"] += 1
        return r

    resp = completion_with_capabilities(
        completion=fake,
        base_kwargs={"model": "moonshot/kimi-k2.5", "messages": [{"role": "user", "content": "q"}]},
        model="moonshot/kimi-k2.5",
        web_search=True,
        deep_thinking=False,
        logger=LOG,
    )
    assert resp.choices[0].message.content == "final"
    assert state["i"] == 2  # 一次工具调用 + 一次最终
    assert state["kwargs"][0]["tools"][0]["function"]["name"] == "$web_search"
    # 第二轮 messages 应带上 assistant(tool_calls) + tool($web_search 结果)
    msgs2 = state["kwargs"][1]["messages"]
    assert any(m.get("role") == "tool" and m.get("name") == "$web_search" for m in msgs2)


def test_native_web_search_rejection_falls_back_to_plain():
    """实测回归：某些 Anthropic 网关拒绝 litellm 的 web_search_options（工具名校验失败）→
    必须回退「能力前的原始调用」（无 web_search_options），绝不把 BadRequestError 抛给生文。"""

    def fake(**kwargs):
        if "web_search_options" in kwargs:
            raise RuntimeError("tools.0.web_search_20250305.name: Input should be 'web_search'")
        return _Resp(_Msg(content="fallback-ok"))

    resp = completion_with_capabilities(
        completion=fake,
        base_kwargs={"model": "anthropic/claude-opus-4-8", "messages": []},
        model="anthropic/claude-opus-4-8",
        web_search=True,
        deep_thinking=False,
        logger=LOG,
    )
    assert resp.choices[0].message.content == "fallback-ok"


def test_deep_thinking_rejection_falls_back_to_plain():
    """深度思考参数被硬拒（非 drop_params 能消化的报错）→ 同样回退原始调用，不拖垮生文。"""

    def fake(**kwargs):
        if "reasoning_effort" in kwargs:
            raise RuntimeError("reasoning not supported by this endpoint")
        return _Resp(_Msg(content="plain-ok"))

    resp = completion_with_capabilities(
        completion=fake,
        base_kwargs={"model": "deepseek/deepseek-v4-flash", "messages": []},
        model="deepseek/deepseek-v4-flash",
        web_search=False,
        deep_thinking=True,
        logger=LOG,
    )
    assert resp.choices[0].message.content == "plain-ok"


def test_moonshot_web_search_failure_falls_back_to_plain():
    def fake(**kwargs):
        if "tools" in kwargs:
            raise RuntimeError("kimi search boom")
        return _Resp(_Msg(content="fallback-ok"))

    resp = completion_with_capabilities(
        completion=fake,
        base_kwargs={"model": "moonshot/kimi-k2.5", "messages": []},
        model="moonshot/kimi-k2.5",
        web_search=True,
        deep_thinking=False,
        logger=LOG,
    )
    assert resp.choices[0].message.content == "fallback-ok"  # 联网失败 → 回退普通生文
