"""MCP 工具英文摘要 → 中文「用处」机翻 + 持久缓存。

设计要点（见 docs 讨论与本模块单测）：
- **只翻没有手写覆盖的工具**：调用方先把命中 `_CURATED_ZH` 的剔掉，这里只收到「真·新工具」。
- **按英文源指纹缓存**：key = sha256(name + 英文摘要)。只有工具新增 / docstring 英文改了才会
  miss → 才真调模型；平时全是缓存命中、零 LLM 调用。缓存落 `GEO_DATA_DIR/cache/mcp_tool_zh.json`，
  跨重启复用（web 单实例，文件足够，不引 DB）。
- **engine 由调用方传入**：model / api_key / base_url / timeout 由调用方用
  `resolve_format_engine(db)`（与 ai_format 同源，prod 能拿到 DB 行里的网关 base_url）解析后传进来，
  本模块只管「缓存 + 调模型 + 解析」，不读 settings、不碰 DB。
- **失败优雅退化**：没配 key / 超时 / 模型返回解析失败 → 整批返回空，调用方退回英文。
  绝不抛、绝不挂、绝不 500。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading

from server.app.core.paths import get_data_dir

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CACHE: dict[str, str] | None = None

_SYS_PROMPT = (
    "你是技术文档翻译。把每条英文「工具说明」翻成简洁的中文（每条≤20字，像功能标签）。"
    "snake_case 标识符和英文专有名词（scope / pipeline / markdown / id / Loop / Tiptap 等）保留英文原样。"
    '只输出一个 JSON 对象，key 是传入的序号字符串、value 是中文译文，例如 {"0":"...","1":"..."}，'
    "不要输出任何额外文字或代码块包裹。"
)


def _fingerprint(name: str, en: str) -> str:
    return hashlib.sha256(f"{name}\x00{en}".encode()).hexdigest()[:16]


def _cache_file():
    return get_data_dir() / "cache" / "mcp_tool_zh.json"


def _load_cache() -> dict[str, str]:
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.loads(_cache_file().read_text("utf-8"))
            if not isinstance(_CACHE, dict):
                _CACHE = {}
        except FileNotFoundError:
            _CACHE = {}
        except Exception:
            logger.warning("MCP 翻译缓存读取失败，按空缓存处理", exc_info=True)
            _CACHE = {}
    return _CACHE


def _save_cache(cache: dict[str, str]) -> None:
    """原子写：先写临时文件再 os.replace（web 单实例，无需跨进程锁）。"""
    path = _cache_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=0), "utf-8")
        os.replace(tmp, path)
    except Exception:
        logger.warning("MCP 翻译缓存写入失败（不致命，下次再翻）", exc_info=True)


def _strip_fences(text: str) -> str:
    """去掉模型可能加的 ```json ... ``` 包裹，留中间 JSON。"""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: s.rfind("```")]
    return s.strip()


def _llm_translate(
    pairs: list[tuple[str, str]],
    *,
    model: str,
    api_key: str | None,
    base_url: str | None,
    timeout: int,
) -> dict[str, str]:
    """批量翻一组 (name, 英文) → {name: 中文}。任何失败返回 {}（调用方退回英文）。"""
    if not api_key or not pairs:
        return {}
    try:
        from litellm import completion

        payload = [{"i": i, "en": en} for i, (_n, en) in enumerate(pairs)]
        resp = completion(
            model=model,
            api_key=api_key,
            base_url=base_url or None,
            messages=[
                {"role": "system", "content": _SYS_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0,
            timeout=min(20, timeout or 20),
        )
        raw = (resp.choices[0].message.content or "").strip()
        data = json.loads(_strip_fences(raw))
        out: dict[str, str] = {}
        for i, (name, _en) in enumerate(pairs):
            zh = None
            if isinstance(data, dict):
                zh = data.get(str(i), data.get(i))
            elif isinstance(data, list) and i < len(data):
                item = data[i]
                zh = item.get("zh") if isinstance(item, dict) else item
            if isinstance(zh, str) and zh.strip():
                out[name] = zh.strip()
        return out
    except Exception:
        logger.warning("MCP 工具机翻失败，退回英文摘要", exc_info=True)
        return {}


def translate_summaries(
    items: list[tuple[str, str]],
    *,
    model: str,
    api_key: str | None,
    base_url: str | None = None,
    timeout: int = 20,
) -> dict[str, str]:
    """给一批 (工具名, 英文摘要)，返回 {工具名: 中文}。命中缓存的不调模型；
    缺失的批量翻一次并写回缓存。失败的条目直接不在返回里（调用方退回英文）。

    engine 参数（model/api_key/base_url/timeout）由调用方用 resolve_format_engine 解析后传入。"""
    if not items:
        return {}
    result: dict[str, str] = {}
    cache = _load_cache()
    misses: list[tuple[str, str, str]] = []  # (name, en, fp)
    for name, en in items:
        fp = _fingerprint(name, en)
        cached = cache.get(fp)
        if cached:
            result[name] = cached
        else:
            misses.append((name, en, fp))
    if not misses:
        return result

    with _LOCK:
        cache = _load_cache()
        # 锁内复查：可能别的线程刚翻过
        still: list[tuple[str, str, str]] = []
        for name, en, fp in misses:
            cached = cache.get(fp)
            if cached:
                result[name] = cached
            else:
                still.append((name, en, fp))
        if still:
            translated = _llm_translate(
                [(n, e) for n, e, _fp in still],
                model=model,
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
            )
            if translated:
                for name, _en, fp in still:
                    zh = translated.get(name)
                    if zh:
                        cache[fp] = zh
                        result[name] = zh
                _save_cache(cache)
    return result
