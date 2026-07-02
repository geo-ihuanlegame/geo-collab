"""MCP 接入指引相关端点。

两组路由分开挂：
- mcp_connect_user_router：/api/mcp/status，user JWT 鉴权（依赖在 main.py include 时通过
  prefix 链路自然继承，与系统 user JWT 路由一致）。
- mcp_connect_health_router：/api/mcp/health，MCP token 鉴权（router-level dependency）。

为什么分两个 router？两条不同的鉴权边界——不能在同一个 router 上同时挂
`Depends(get_current_user)` 和 `Depends(require_mcp_token)`。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from server.app.core.config import get_settings
from server.app.core.mcp_auth import require_mcp_token

logger = logging.getLogger(__name__)

# 仅给 server/mcp/server.py 的启动断言当「下限」用（注册数必须 ≥ 此值）。
# 页面展示的工具数走 status.tools_count = len(tools)（实时内省），不再读这个常量。
# 增减 MCP tool 时同步这里，避免断言把正常启动误判成双实例 bug。
MCP_TOOLS_COUNT = 21

# 手写中文「用处」覆盖表：命中则优先用（质量比机翻好），未命中的工具走机翻兜底。
# 加了新工具不必动这里——机翻会自动补；想给某工具更准的中文再来加一行。
_CURATED_ZH: dict[str, str] = {
    # catalog（只读查询）
    "list_articles": "按状态 / 审核状态分页查文章列表",
    "list_question_pools": "列出所有问题池（飞书同步的选题库）",
    "list_question_items": "查某问题池下的问题条目（可按分类过滤）",
    "list_prompt_templates": "按 scope 列出提示词模板",
    "list_pipelines": "列出所有 pipeline（智能体 / 工作流）",
    "list_accounts": "列出发布账号（可按平台 / 是否可分发过滤）",
    "get_article": "按 id 取单篇文章全文（Tiptap / HTML / 纯文本）",
    "list_today_loop_articles": "统计窗口内 /goal Loop 已生成且已决策的文章（停止条件用）",
    "list_stock_categories": "列出图片库栏目（配图选 main_category_id 用）",
    # action（写操作）
    "save_article": "把 Claude 写好的 markdown 文章入库（零配置生文）",
    "illustrate_article": "给文章正文按位置插入图库选图",
    "submit_review_decision": "写入一条自动审核决策（不改最终人审状态）",
    "notify_feishu": "发送飞书 webhook 通知",
    "set_review_status": "修改文章审核状态（pending / approved）",
    "create_distribute_task": "建 article_round_robin 分发任务（轮询派号发文）",
    "install_loop_skills": "拉取 /goal Loop skill 包供本地安装",
    "ai_illustrate_article": "AI 智能配图 + 自动封面（对齐 Web UI「AI 配图」）",
    # meta（评估 / 回流）
    "score_recent_articles": "用 ai_format 模型给文章批量 LLM 评分",
    "get_template_performance": "聚合某提示词模板产出文章的表现指标",
    "get_account_performance": "聚合某账号已发布文章的表现指标",
    "record_publish_metrics": "回流写入发布记录的阅读 / 点赞等指标",
}


class McpToolInfo(BaseModel):
    name: str
    group: str  # catalog / action / meta —— 由注册函数所在模块推出
    summary: str  # 工具 docstring 首行（英文）
    summary_zh: str  # 中文「用处」：手写覆盖 → 机翻缓存 → 英文兜底


class McpStatusResponse(BaseModel):
    configured: bool
    suggested_base_url: str
    tools_count: int  # = len(tools)，实时内省，前端段①与右面板同源不漂移
    tools: list[McpToolInfo] = []


class McpHealthResponse(BaseModel):
    ok: bool


def _registered_tools() -> list[McpToolInfo]:
    """内省 FastMCP 活注册表 → (name, group, 英文首行) 列表，按 (group, name) 排序。

    与 server/mcp/server.py 的启动断言读同一个 `mcp._tool_manager._tools`，
    保证展示列表永远等于实际注册的工具。
    """
    # 懒导入：server.mcp.server 顶部 import 了本模块，顶层 import 会循环依赖。
    from server.mcp.server import mcp

    infos: list[McpToolInfo] = []
    for name, tool in mcp._tool_manager._tools.items():
        module = getattr(tool.fn, "__module__", "") or ""
        group = module.rsplit(".", 1)[-1]  # server.mcp.tools.catalog -> "catalog"
        summary = next(
            (line.strip() for line in (tool.description or "").splitlines() if line.strip()),
            "",
        )
        infos.append(McpToolInfo(name=name, group=group, summary=summary, summary_zh=summary))
    infos.sort(key=lambda i: (i.group, i.name))
    return infos


def _attach_zh(infos: list[McpToolInfo]) -> None:
    """给每条填 summary_zh：手写覆盖 → 机翻（仅未覆盖者）→ 英文兜底。

    手写覆盖了现有全部工具，故 to_mt 平时为空 → 一次 LLM 都不调；只有新增工具才触发机翻。
    """
    to_mt = [(i.name, i.summary) for i in infos if i.name not in _CURATED_ZH]
    zh: dict[str, str] = {}
    if to_mt:
        try:
            # engine 与 ai_format 同源解析（短生命周期 session），prod 能拿到 DB 行里的网关 base_url
            from server.app.db.session import SessionLocal
            from server.app.modules.ai_models.service import resolve_format_engine

            from .tool_translate import translate_summaries

            with SessionLocal() as db:
                model, key, base_url, timeout = resolve_format_engine(db, None)
            zh = translate_summaries(
                to_mt, model=model, api_key=key, base_url=base_url, timeout=timeout
            )
        except Exception:
            logger.warning("MCP 工具机翻调用异常，退回英文", exc_info=True)
    for i in infos:
        i.summary_zh = _CURATED_ZH.get(i.name) or zh.get(i.name) or i.summary


# user JWT 鉴权（依赖在 main.py include_router 时注入）
mcp_connect_user_router = APIRouter()


@mcp_connect_user_router.get("/status", response_model=McpStatusResponse)
def get_mcp_status(request: Request) -> McpStatusResponse:
    """[user] MCP 接入状态：是否配置 token、推荐 base_url、已注册工具列表（含中文用处）。

    工具列表实时内省注册表，tools_count = len(tools)。前端「刷新状态」一键拿全部，
    页面只需一个刷新、一个接口。
    """
    settings = get_settings()
    tools = _registered_tools()
    _attach_zh(tools)
    return McpStatusResponse(
        configured=bool(settings.mcp_token),
        suggested_base_url=str(request.base_url).rstrip("/"),
        tools_count=len(tools),
        tools=tools,
    )


# MCP token 鉴权（router-level dependency）
mcp_connect_health_router = APIRouter(dependencies=[Depends(require_mcp_token)])


@mcp_connect_health_router.get("/health", response_model=McpHealthResponse)
def get_mcp_health() -> McpHealthResponse:
    """[MCP] 健康探针：仅用于 Claude Code Loop 启动时校验 token + base_url 联通。"""
    return McpHealthResponse(ok=True)
