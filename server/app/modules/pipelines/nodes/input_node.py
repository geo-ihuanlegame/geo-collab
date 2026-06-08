"""input 源节点：把 config 里手填的 question_text 原样作为输出，供下游 ai_generate / ai_compose 用。"""

from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register


def run_input(ctx: NodeRunContext) -> NodeResult:
    text = (ctx.config or {}).get("question_text", "")
    return NodeResult(output={"question_text": text}, article_ids=[])


register("input", run_input)
