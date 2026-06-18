"""头条 creator-ID（media_id）纯解析层。

移植自 spike `E:\\1\\toutiao_creator_id_probe.py` 的**纯解析**部分：从 creator 平台
返回的 JSON / 文本 / DOM 文本里抽出 8–30 位纯数字的「头条号ID」（=media_id）。

**纯函数、无浏览器、无 I/O** —— sync/async Playwright I/O 适配层是后续阶段的事，本文件
绝不 import playwright，可脱浏览器直接单测（见设计稿 §3 纯解析层）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# 头条号ID：8–30 位、首位非 0 的纯数字（正则与 spike / 设计稿 §2.1 一致）
NUMERIC_ID_RE = re.compile(r"^[1-9]\d{7,29}$")
MEDIA_ID_PAIR_RE = re.compile(r'"media_id"\s*:\s*"?([1-9]\d{7,29})"?')
# 「头条号ID」label（头条号 = 头条号）后 80 字符内的首个合法数字
LABEL_ID_RE = re.compile(r"头条号\s*ID[^\d]{0,80}([1-9]\d{7,29})", re.I)

# 「头条号ID」DOM label 常量，供 I/O 适配层向 page.evaluate 传参用
TOUTIAO_ID_LABEL = "头条号ID"


@dataclass(frozen=True)
class CreatorIdResult:
    value: str
    source: str
    evidence: str


def is_numeric_id(value: Any) -> bool:
    """是否为合法头条号ID（8–30 位、首位非 0 的纯数字）。None / 空 / 非数字串均 False。"""
    if value is None:
        return False
    return bool(NUMERIC_ID_RE.fullmatch(str(value).strip()))


def extract_media_id_from_json(value: Any, path: str = "$") -> CreatorIdResult | None:
    """深度优先遍历任意 JSON 结构，返回首个 ``media_id`` 合法数字字段。"""
    if isinstance(value, dict):
        media_id = value.get("media_id")
        if is_numeric_id(media_id):
            return CreatorIdResult(str(media_id), f"json:{path}.media_id", f"media_id={media_id}")

        for key, child in value.items():
            found = extract_media_id_from_json(child, f"{path}.{key}")
            if found:
                return found

    if isinstance(value, list):
        for index, child in enumerate(value):
            found = extract_media_id_from_json(child, f"{path}[{index}]")
            if found:
                return found

    return None


def extract_media_id_from_text(text: str, source: str) -> CreatorIdResult | None:
    """文本兜底：先找 ``"media_id": <id>`` 对，再找「头条号ID」label 后的数字。"""
    media_match = MEDIA_ID_PAIR_RE.search(text or "")
    if media_match:
        return CreatorIdResult(media_match.group(1), source, media_match.group(0))

    label_match = LABEL_ID_RE.search(text or "")
    if label_match:
        return CreatorIdResult(label_match.group(1), source, label_match.group(0))

    return None


def parse_creator_info_response(text: str, source: str) -> CreatorIdResult | None:
    """解析 creator-info 接口返回体：能 JSON 解析则走结构化抽取，否则退化到文本兜底。

    JSON 解析成功但 ``media_id`` 缺失时，仍对原始文本做一次文本兜底（与 spike 行为一致）。
    """
    text = text or ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return extract_media_id_from_text(text, source)

    found = extract_media_id_from_json(parsed)
    if found:
        return CreatorIdResult(found.value, source, found.evidence)

    return extract_media_id_from_text(text, source)


def normalize_dom_scan_result(result: Any, source: str) -> CreatorIdResult | None:
    """把 DOM label 扫描（``page.evaluate`` 返回的 ``{value, evidence}``）规范成结果。

    纯函数：I/O 适配层负责跑 evaluate 拿到 dict，本函数只做数字校验与封装。
    """
    if isinstance(result, dict) and is_numeric_id(result.get("value")):
        return CreatorIdResult(
            str(result["value"]),
            source,
            str(result.get("evidence") or ""),
        )
    return None
