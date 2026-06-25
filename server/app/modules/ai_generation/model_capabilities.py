"""生文「模型能力」开关 → litellm 调用参数的映射（按 provider 分叉，全 best-effort）。

两个能力都不能拖垮生文：能力不支持/调用失败时回退到不带该能力的普通 completion，绝不抛给上层。

- 深度思考(deep_thinking)：统一传 litellm `reasoning_effort`。配 `drop_params=True`，不支持推理的
  模型会被静默丢弃该参数。Anthropic 扩展思考要求 temperature≠0，故 thinking 时去掉 temperature=0。
- 联网搜索(web_search)：各家原生，按 provider 分叉
    · Anthropic / OpenAI / Gemini / xAI → `web_search_options={}`（litellm 统一翻译成各家联网工具）
    · Moonshot / Kimi → `$web_search` builtin 工具 + 工具调用循环（litellm 的 web_search_options 覆盖不到）
    · 豆包 / Doubao → litellm volcengine 不支持 web_search_options（其联网是 tools/联网内容插件，
      schema 待确认），暂按「其它」静默忽略，仅识别 provider 让深度思考与日志标注正确
    · 其它 provider → 不动，靠 `drop_params=True` 静默忽略

provider 判定只看 model 串前缀/关键字；新增模型族在 `_provider_of` 补一行即可。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

# 推理力度：留一个常量旋钮，未来可做成每节点可配。high 给最充分的思考预算。
REASONING_EFFORT = "high"
# Moonshot $web_search 工具循环上限，兜底防模型反复请求搜索导致死循环。
MAX_WEB_SEARCH_ROUNDS = 3
# Anthropic 官方端点。api_base 指向官方之外 = 走中转网关（CRS），其服务端 web_search 多不支持。
_OFFICIAL_ANTHROPIC_HOSTS = ("api.anthropic.com",)


def _provider_of(model: str) -> str:
    """从 litellm model 串粗判 provider 族。仅用于能力分叉，未知一律 'other'（=不加能力，靠 drop_params 兜底）。"""
    m = (model or "").lower()
    if m.startswith("moonshot/") or "kimi" in m:
        return "moonshot"
    if m.startswith(("volcengine/", "doubao/")) or "doubao" in m:
        return "doubao"  # litellm 不支持其 web_search_options，联网暂静默忽略；识别仅为深度思考/日志标注
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


def _api_base_of(base_kwargs: dict[str, Any]) -> str | None:
    """取调用的 base_url（litellm 字段名 api_base，兼容 base_url）。None = 未配 = 官方直连。"""
    return base_kwargs.get("api_base") or base_kwargs.get("base_url")


def _extract_reasoning_text(response: Any) -> str | None:
    """取模型这次返回的思考/推理内容。litellm 把各家思考统一归一到 message.reasoning_content；
    另兼容 reasoning（部分版本）与 Anthropic 的 thinking_blocks。无则 None。"""
    try:
        msg = response.choices[0].message
    except (AttributeError, IndexError, TypeError):
        return None
    text = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
    if text:
        return str(text)
    blocks = getattr(msg, "thinking_blocks", None)
    if blocks:
        parts = [b.get("thinking", "") if isinstance(b, dict) else str(b) for b in blocks]
        joined = "".join(p for p in parts if p)
        return joined or None
    return None


def _web_search_was_used(response: Any) -> bool:
    """best-effort：从响应里找模型「真的联网检索过」的证据——引用注解(url_citation) 或服务端工具调用
    计数(server_tool_use.web_search_requests)。找到＝True。
    注意：False 不等于「不支持」，模型也可能自行判定无需检索；该语义在日志文案里区分。"""
    try:
        msg = response.choices[0].message
    except (AttributeError, IndexError, TypeError):
        return False
    for a in getattr(msg, "annotations", None) or []:
        t = a.get("type") if isinstance(a, dict) else getattr(a, "type", None)
        if t and "url_citation" in str(t):
            return True
    usage = getattr(response, "usage", None)
    stu = getattr(usage, "server_tool_use", None) if usage else None
    reqs = getattr(stu, "web_search_requests", None) if stu else None
    return isinstance(reqs, int) and reqs > 0


def _thinking_status(response: Any, deep_thinking: bool) -> str:
    """深度思考的最终「是否使用」文案：响应带思考内容＝已使用；不带＝模型不支持/未返回(已被 drop_params 忽略)。"""
    if not deep_thinking:
        return "未请求"
    reasoning = _extract_reasoning_text(response)
    if reasoning:
        return f"已使用（响应含思考内容 {len(reasoning)} 字）"
    return "未使用（模型不支持或未返回思考，reasoning_effort 已被 drop_params 忽略）"


def _log_capability_result(
    response: Any, model: str, logger: Any, deep_thinking: bool, search_status: str
) -> None:
    """生文返回后打一条「实际结果」：明确告诉运营本次每个能力到底用没用。无论模型支持与否都打。"""
    logger.info(
        "[生文能力·结果] model=%s | 深度思考=%s | 联网搜索=%s",
        model,
        _thinking_status(response, deep_thinking),
        search_status,
    )


def _is_anthropic_relay(provider: str, base_kwargs: dict[str, Any]) -> bool:
    """provider 为 anthropic 且 api_base 指向官方之外（含端口/带认证前缀都归一掉）即视为中转网关。

    未配 api_base = litellm 默认官方直连，不算中转。
    """
    if provider != "anthropic":
        return False
    api_base = _api_base_of(base_kwargs)
    if not api_base:
        return False
    host = urlsplit(str(api_base)).netloc.lower().split("@")[-1].split(":")[0]
    return host not in _OFFICIAL_ANTHROPIC_HOSTS


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
    requested_web = web_search  # 原始请求；下方失败回退会改 web_search，结果日志要按原始请求措辞

    # 联网搜索：先判定本次到底会不会真发送 + 走哪条渠道 / 为何跳过。
    # 方案2：Anthropic 经中转网关不支持服务端联网（litellm 翻译的 web_search_20250305 会被 name 校验拒），
    # 主动跳过，免去每次注定失败的请求 + 重试；官方直连（未配 api_base）仍尝试。
    search_sent = False
    search_channel = ""
    skip_reason = ""
    if web_search:
        if _is_anthropic_relay(provider, base_kwargs):
            skip_reason = (
                f"Anthropic 中转网关(base_url={_api_base_of(base_kwargs)})不支持服务端联网"
            )
        elif provider == "moonshot":
            search_sent, search_channel = True, "Moonshot $web_search 工具循环"
        elif _supports_native_web_search_options(provider):
            search_sent, search_channel = True, "web_search_options"
        else:
            skip_reason = f"provider={provider} 无原生联网支持（litellm drop_params 静默忽略）"

    # 调用前打「请求计划」：万一后续整段抛异常，至少留下本次想用什么能力。
    logger.info(
        "[生文能力·请求] model=%s provider=%s | 联网搜索=%s%s | 深度思考=%s%s",
        model,
        provider,
        requested_web,
        (
            f"，走 {search_channel}"
            if search_sent
            else (f"，跳过：{skip_reason}" if requested_web else "")
        ),
        deep_thinking,
        f"（reasoning_effort={REASONING_EFFORT}）" if deep_thinking else "",
    )

    # 执行联网路径，返回后判定「实际是否用了」并打结果日志。
    if search_sent:
        try:
            if provider == "moonshot":
                resp, rounds = _moonshot_web_search_loop(completion, thinking_kwargs, logger)
                search_status = (
                    f"已使用（$web_search，{rounds} 轮检索）"
                    if rounds > 0
                    else "未使用（已启用 $web_search，但模型本次未触发检索）"
                )
            else:  # native web_search_options
                # litellm 的 web_search_options 在部分 provider/网关上可能因工具名/版本不符被拒
                # （实测某 Anthropic 网关报 web_search_20250305 name 校验失败）——失败即去掉联网重试。
                resp = completion(**{**thinking_kwargs, "web_search_options": {}})
                search_status = (
                    "已使用（检测到检索引用/证据）"
                    if _web_search_was_used(resp)
                    else "已启用（web_search_options），但本次未检测到检索证据（模型可能判定无需联网）"
                )
            _log_capability_result(resp, model, logger, deep_thinking, search_status)
            return resp
        except Exception as exc:  # noqa: BLE001 — 联网失败不拖垮生文，去掉联网、保留深度思考重试
            logger.warning("联网搜索失败，去掉联网保留深度思考重试：%s", exc)
            skip_reason = f"联网调用失败已回退（{exc}）"

    # 基线：没走联网（未请求 / 被跳过 / 无原生支持 / 联网失败回退）。
    if not requested_web:
        baseline_search_status = "未请求"
    else:
        baseline_search_status = f"未使用（{skip_reason or '该模型无原生联网支持'}）"
    try:
        resp = completion(**thinking_kwargs)
        _log_capability_result(resp, model, logger, deep_thinking, baseline_search_status)
        return resp
    except Exception as exc:  # noqa: BLE001 — 深度思考也不支持/失败 → 退回能力前的原始调用
        logger.warning("深度思考调用失败，回退普通生文：%s", exc)
        resp = completion(**dict(base_kwargs))  # 无能力基线：深度思考必然未生效
        thinking_note = "未使用（深度思考调用失败，已回退普通生文）" if deep_thinking else "未请求"
        logger.info(
            "[生文能力·结果] model=%s | 深度思考=%s | 联网搜索=%s",
            model,
            thinking_note,
            baseline_search_status,
        )
        return resp


def _moonshot_web_search_loop(
    completion: Callable[..., Any], kwargs: dict[str, Any], logger: Any
) -> tuple[Any, int]:
    """Moonshot $web_search builtin：模型发起 $web_search tool_call 时，把它的 arguments 原样回填为
    tool 结果（Moonshot 服务端据此执行搜索并计费），循环至模型不再请求搜索。rounds 用尽则返回最后一次。

    返回 (response, search_rounds)：search_rounds=模型实际触发联网检索的轮数（0=本次没联网）。
    """
    kwargs = dict(kwargs)
    messages = list(kwargs.pop("messages"))
    kwargs["tools"] = [{"type": "builtin_function", "function": {"name": "$web_search"}}]
    last: Any = None
    rounds = 0
    for _ in range(MAX_WEB_SEARCH_ROUNDS):
        last = completion(messages=messages, **kwargs)
        msg = last.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            return last, rounds
        rounds += 1
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
        logger.info("Kimi $web_search 第 %s 轮：模型请求联网检索", rounds)
    return last, rounds
