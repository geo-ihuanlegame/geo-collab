"""百度千帆「AI 搜索」客户端：按关键词搜真实横版图，供 AI配图「联网兜底」用。

为什么是这个接口：百度没有官方「关键词搜网图」API，官方图像搜索是以图搜图（反向）。
千帆 AI 搜索（/v2/ai_search/web_search）的 resource_type_filter:image 能按关键词回真实网图，
返回 references[].image.url（实测可直接下载、无防盗链）。详见 spike 结论。

设计要点（best-effort，绝不拖垮配图）：
  - Key 缺失 / 网络 / 解析失败 → 记日志、返回空，不抛给上层。
  - 横版过滤用接口返回的 width/height（绕开 webp 量不出尺寸的问题），不依赖下载后解析。
  - 结构化 ratio 过滤实测无效，靠 query 文本（「游戏名 横屏壁纸」）拿横版，再用宽高兜一道。
  - 下载按 magic-bytes 定类型（不信 Content-Type，见过 octet-stream 的真图）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 实测：query 文本是有效杠杆，结构化 ratio 过滤被忽略。横版意图写进 query。
# 默认搜图关键词模板，可被数据库里 image_search scope 的提示词覆盖（见 ai_format 配图链路）。
# {game} 占位符=游戏名；保留"横版"维持下方 landscape_only() 横版过滤意图。
DEFAULT_IMAGE_SEARCH_QUERY = "{game} 横版 官方宣传图"
_GAME_PLACEHOLDER = "{game}"
_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 与平台单图上限 MAX_ASSET_BYTES 对齐
_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@dataclass
class BaiduImage:
    url: str
    width: int
    height: int
    source_url: str  # 来源页（版权溯源）
    title: str


def _to_int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def parse_image_references(data: dict) -> list[BaiduImage]:
    """从 AI 搜索返回里抽图片：references[].image.url(+width/height)。纯函数，便于测试。"""
    out: list[BaiduImage] = []
    for ref in data.get("references") or []:
        if not isinstance(ref, dict):
            continue
        img = ref.get("image")
        if not isinstance(img, dict):
            continue
        url = img.get("url")
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        out.append(
            BaiduImage(
                url=url,
                width=_to_int(img.get("width")),
                height=_to_int(img.get("height")),
                source_url=str(ref.get("url") or ""),
                title=str(ref.get("title") or ""),
            )
        )
    return out


def landscape_only(images: list[BaiduImage]) -> list[BaiduImage]:
    """只留横版（宽>高），按面积从大到小排序；缺尺寸的丢弃（无法判定横竖）。"""
    usable = [im for im in images if im.width > im.height > 0]
    usable.sort(key=lambda im: im.width * im.height, reverse=True)
    return usable


def sniff_image_mime(data: bytes) -> str | None:
    """按文件头判图片类型（不信 Content-Type）。认 JPEG/PNG/WebP/GIF，否则 None。"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def build_search_query(game_name: str, query_template: str = DEFAULT_IMAGE_SEARCH_QUERY) -> str:
    """按模板拼搜索词。含 {game} 占位符则替换；否则按「游戏名 模板内容」空格拼接。纯函数，便于测试。"""
    game = (game_name or "").strip()
    template = (query_template or "").strip() or DEFAULT_IMAGE_SEARCH_QUERY
    if _GAME_PLACEHOLDER in template:
        return template.replace(_GAME_PLACEHOLDER, game).strip()
    return f"{game} {template}".strip()


def search_landscape_images(
    game_name: str, *, query_template: str = DEFAULT_IMAGE_SEARCH_QUERY, top_k: int = 15
) -> list[BaiduImage]:
    """搜某游戏的横版图，返回横版候选（已按面积降序）。失败返回 []（best-effort）。

    query_template 来自数据库可编辑的搜图关键词模板（image_search scope），缺省回退默认模板。
    """
    from server.app.core.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    api_key = settings.baidu_api_key
    if not api_key:
        logger.warning("联网兜底跳过：未配置 GEO_BAIDU_API_KEY")
        return []

    query = build_search_query(game_name, query_template)
    bearer = f"Bearer {api_key}"
    body = {
        "messages": [{"role": "user", "content": query}],
        "search_source": "baidu_search_v2",
        "resource_type_filter": [{"type": "image", "top_k": top_k}],
    }
    try:
        import httpx

        resp = httpx.post(
            settings.baidu_ai_search_url,
            headers={
                "Authorization": bearer,
                "X-Appbuilder-Authorization": bearer,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=settings.baidu_ai_search_timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("联网兜底搜图失败（query=%s）：%s", query, exc)
        return []

    images = landscape_only(parse_image_references(data))
    logger.info("联网兜底搜图 query=%s 命中横版 %d 张", query, len(images))
    return images


def download_image(url: str) -> tuple[bytes, str] | None:
    """下载并按 magic-bytes 校验，返回 (bytes, mime)；非图片/超限/失败返回 None。"""
    try:
        import httpx

        resp = httpx.get(
            url,
            headers={"User-Agent": _BROWSER_UA, "Referer": "https://image.baidu.com/"},
            timeout=30,
            follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.content
    except Exception as exc:
        logger.warning("联网兜底下载失败 url=%s：%s", url, exc)
        return None

    if not data or len(data) > _MAX_IMAGE_BYTES:
        logger.warning("联网兜底下载丢弃 url=%s：空或超过 %d 字节", url, _MAX_IMAGE_BYTES)
        return None
    mime = sniff_image_mime(data)
    if mime is None or mime not in _ALLOWED_MIME:
        logger.warning("联网兜底下载丢弃 url=%s：非受支持图片类型", url)
        return None
    return data, mime
