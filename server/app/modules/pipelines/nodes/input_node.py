from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register


def run_input(ctx: NodeRunContext) -> NodeResult:
    text = (ctx.config or {}).get("question_text", "")
    return NodeResult(output={"question_text": text}, article_ids=[])


register("input", run_input)
