"""生文「模型能力」开关 → litellm 调用参数的映射（按 provider 分叉，全 best-effort）。

两个能力都不能拖垮生文：能力不支持/调用失败时回退到不带该能力的普通 completion，绝不抛给上层。

- 深度思考(deep_thinking)：统一传 litellm `reasoning_effort`。配 `drop_params=True`，不支持推理的
  模型会被静默丢弃该参数。Anthropic 扩展思考要求 temperature≠0，故 thinking 时去掉 temperature=0。
- 联网搜索(web_search)：各家原生，按 provider 分叉
    · Anthropic / OpenAI / Gemini / xAI → `web_search_options={}`（litellm 统一翻译成各家联网工具）
    · Moonshot / Kimi → `$web_search` builtin 工具 + 工具调用循环（litellm 的 web_search_options 覆盖不到）
    · 其它 provider → 不动，靠 `drop_params=True` 静默忽略

provider 判定只看 model 串前缀/关键字；新增模型族在 `_provider_of` 补一行即可。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# 推理力度：留一个常量旋钮，未来可做成每节点可配。high 给最充分的思考预算。
REASONING_EFFORT = "high"
# Moonshot $web_search 工具循环上限，兜底防模型反复请求搜索导致死循环。
MAX_WEB_SEARCH_ROUNDS = 3


def _provider_of(model: str) -> str:
    """从 litellm model 串粗判 provider 族。仅用于能力分叉，未知一律 'other'（=不加能力，靠 drop_params 兜底）。"""
    m = (model or "").lower()
    if m.startswith("moonshot/") or "kimi" in m:
        return "moonshot"
    if m.startswith("anthropic/") or "claude" in m:
        return "anthropic"
    if m.startswith("openai/") or m.startswith("gpt-") or m.startswith(("o1", "o3", "o4")):
        return "openai"
    if m.startswith(("gemini/", "vertex_ai/")):
        return "gemini"
    if m.startswith("xai/") or m.startswith("grok"):
        return "xai"
    return "other"


def _apply_deep_thinking(kwargs: dict[str, Any], model: str) -> None:
    """注入 reasoning_effort；Anthropic 扩展思考与 temperature=0 冲突 → 去掉 temperature。"""
    kwargs["reasoning_effort"] = REASONING_EFFORT
    if _provider_of(model) == "anthropic":
        kwargs.pop("temperature", None)  # 扩展思考要求 temperature 为默认(1)，不能传 0


def _supports_native_web_search_options(provider: str) -> bool:
    return provider in ("anthropic", "openai", "gemini", "xai")


def completion_with_capabilities(
    *,
    completion: Callable[..., Any],
    base_kwargs: dict[str, Any],
    model: str,
    web_search: bool,
    deep_thinking: bool,
    logger: Any,
) -> Any:
    """按能力开关调 `completion`(=litellm.completion)，返回其 response。

    任一能力路径异常 → 回退到不带该能力的普通 completion，绝不抛。`base_kwargs` 至少含 model/messages。
    """
    # 无任何能力开关 → 原样调用，与接入本能力前完全一致（scheme/MCP/ai_generate 等默认路径零行为变化）。
    if not web_search and not deep_thinking:
        return completion(**dict(base_kwargs))

    # thinking_kwargs = 原始调用 + drop_params + （可选）深度思考。它是「不带联网」的基线，
    # 联网层失败时回退到它（保住深度思考）。drop_params 让不支持的参数静默丢弃、不报错。
    thinking_kwargs = dict(base_kwargs)
    thinking_kwargs["drop_params"] = True
    if deep_thinking:
        _apply_deep_thinking(thinking_kwargs, model)

    # 分层 best-effort 降级：联网层失败 → 保留深度思考重试；深度思考也失败 → 退回能力前的原始调用。
    provider = _provider_of(model)
    if web_search:
        try:
            if provider == "moonshot":
                return _moonshot_web_search_loop(completion, thinking_kwargs, logger)
            if _supports_native_web_search_options(provider):
                # litellm 的 web_search_options 在部分 provider/网关上可能因工具名/版本不符被拒
                # （实测某 Anthropic 网关报 web_search_20250305 name 校验失败）——失败即去掉联网重试。
                return completion(**{**thinking_kwargs, "web_search_options": {}})
        except Exception as exc:  # noqa: BLE001 — 联网失败不拖垮生文，去掉联网、保留深度思考重试
            logger.warning("联网搜索失败，去掉联网保留深度思考重试：%s", exc)

    try:
        return completion(**thinking_kwargs)
    except Exception as exc:  # noqa: BLE001 — 深度思考也不支持/失败 → 退回能力前的原始调用
        logger.warning("深度思考调用失败，回退普通生文：%s", exc)
        return completion(**dict(base_kwargs))


def _moonshot_web_search_loop(
    completion: Callable[..., Any], kwargs: dict[str, Any], logger: Any
) -> Any:
    """Moonshot $web_search builtin：模型发起 $web_search tool_call 时，把它的 arguments 原样回填为
    tool 结果（Moonshot 服务端据此执行搜索并计费），循环至模型不再请求搜索。rounds 用尽则返回最后一次。
    """
    kwargs = dict(kwargs)
    messages = list(kwargs.pop("messages"))
    kwargs["tools"] = [{"type": "builtin_function", "function": {"name": "$web_search"}}]
    last = None
    for _ in range(MAX_WEB_SEARCH_ROUNDS):
        last = completion(messages=messages, **kwargs)
        msg = last.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            return last
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": getattr(msg, "content", "") or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ],
        }
        # 保留推理模型的 reasoning_content：Moonshot 推理模型要求多轮历史里带它，
        # 否则 litellm 注占位符并告警、且降低多轮保真。原样回带。
        reasoning_content = getattr(msg, "reasoning_content", None)
        if reasoning_content:
            assistant_msg["reasoning_content"] = reasoning_content
        messages.append(assistant_msg)
        for tc in tool_calls:
            if tc.function.name == "$web_search":
                # builtin：原样回填 arguments，由 Moonshot 服务端执行检索
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": "$web_search",
                        "content": tc.function.arguments,
                    }
                )
        logger.info("Kimi $web_search 第 %s 轮：模型请求联网检索", _ + 1)
    return last
