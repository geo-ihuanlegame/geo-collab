import { useEffect, useRef, useState } from "react";
import { Sparkles, ExternalLink } from "lucide-react";
import {
  listSkills,
  listPromptTemplates,
  startGeneration,
  getGenerationSession,
} from "../../api/ai-generation";
import { getArticle } from "../../api/articles";
import { useToast } from "../../components/Toast";
import type { Skill, PromptTemplate, GenerationSession, Article } from "../../types";

export function GenerateTab({ onNavigateToContent }: { onNavigateToContent: () => void }) {
  const { toast } = useToast();

  const [skills, setSkills] = useState<Skill[]>([]);
  const [prompts, setPrompts] = useState<PromptTemplate[]>([]);
  const [selectedSkillId, setSelectedSkillId] = useState<number | "">("");
  const [selectedPromptId, setSelectedPromptId] = useState<number | "">("");
  const [extraInstruction, setExtraInstruction] = useState("");

  const [session, setSession] = useState<GenerationSession | null>(null);
  const [isGenerating, setIsGenerating] = useState(false);
  const [generatedArticles, setGeneratedArticles] = useState<Article[]>([]);
  const [hasResult, setHasResult] = useState(false);

  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    Promise.all([listSkills(), listPromptTemplates("generation")]).then(([s, p]) => {
      setSkills(s.filter((x) => x.is_enabled));
      setPrompts(p.filter((x) => x.is_enabled));
    });
  }, []);

  function stopPolling() {
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }

  useEffect(() => () => stopPolling(), []);

  async function fetchArticles(ids: number[]): Promise<Article[]> {
    const results = await Promise.allSettled(ids.map((id) => getArticle(id)));
    return results
      .flatMap((r) => (r.status === "fulfilled" ? [r.value] : []));
  }

  async function handleGenerate() {
    if (!selectedSkillId || !selectedPromptId) {
      toast("请选择 Skill 和提示词", "error");
      return;
    }
    setIsGenerating(true);
    setHasResult(false);
    setGeneratedArticles([]);
    setSession(null);

    try {
      const { session_id } = await startGeneration({
        skill_id: selectedSkillId as number,
        prompt_template_id: selectedPromptId as number,
        extra_instruction: extraInstruction || undefined,
      });

      pollTimerRef.current = setInterval(async () => {
        try {
          const s = await getGenerationSession(session_id);
          setSession(s);
          if (s.status === "done") {
            stopPolling();
            setIsGenerating(false);
            setHasResult(true);
            const articles = await fetchArticles(s.article_ids);
            setGeneratedArticles(articles);
          } else if (s.status === "failed") {
            stopPolling();
            setIsGenerating(false);
            setHasResult(true);
            toast(s.error_message || "生成失败，请重试", "error");
          }
        } catch {
          // 网络抖动，继续轮询
        }
      }, 2000);
    } catch (err) {
      setIsGenerating(false);
      toast(err instanceof Error ? err.message : "启动生成失败", "error");
    }
  }

  function handleClear() {
    stopPolling();
    setIsGenerating(false);
    setHasResult(false);
    setSession(null);
    setGeneratedArticles([]);
    setSelectedSkillId("");
    setSelectedPromptId("");
    setExtraInstruction("");
  }

  return (
    <div className="aiGenerateLayout">
      {/* Left: config */}
      <div className="aiConfigPanel">
        <div className="aiFormGroup">
          <label className="aiFormLabel">Skill</label>
          <select
            className="aiSelect"
            value={selectedSkillId}
            onChange={(e) => setSelectedSkillId(e.target.value ? Number(e.target.value) : "")}
            disabled={isGenerating}
          >
            <option value="">请选择技能…</option>
            {skills.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </div>

        <div className="aiFormGroup">
          <label className="aiFormLabel">提示词</label>
          <select
            className="aiSelect"
            value={selectedPromptId}
            onChange={(e) => setSelectedPromptId(e.target.value ? Number(e.target.value) : "")}
            disabled={isGenerating}
          >
            <option value="">请选择提示词…</option>
            {prompts.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </div>

        <div className="aiFormGroup">
          <label className="aiFormLabel">补充说明（可选）</label>
          <textarea
            className="aiTextarea"
            placeholder="追加到生成指令末尾的补充信息…"
            value={extraInstruction}
            onChange={(e) => setExtraInstruction(e.target.value)}
            disabled={isGenerating}
          />
        </div>

        <div className="aiConfigActions">
          <button
            className="aiGenerateBtn"
            type="button"
            onClick={handleGenerate}
            disabled={isGenerating || !selectedSkillId || !selectedPromptId}
          >
            <Sparkles size={15} />
            {isGenerating ? "生成中…" : "生成"}
          </button>
          {hasResult && (
            <button className="secondaryButton" type="button" onClick={handleClear}>
              清空
            </button>
          )}
        </div>
      </div>

      {/* Right: result */}
      <div className="aiResultPanel">
        {!isGenerating && !hasResult && <EmptyState />}
        {isGenerating && <LoadingState />}
        {hasResult && !isGenerating && session?.status === "done" && (
          <ResultList articles={generatedArticles} onNavigateToContent={onNavigateToContent} />
        )}
        {hasResult && !isGenerating && session?.status === "failed" && (
          <div className="aiEmptyState">
            <p style={{ color: "var(--red)" }}>生成失败：{session.error_message || "未知错误"}</p>
          </div>
        )}
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="aiEmptyState">
      <Sparkles size={40} style={{ opacity: 0.2 }} />
      <p className="aiEmptyText">选择 Skill 和提示词，点击「生成」开始创作</p>
    </div>
  );
}

function LoadingState() {
  return (
    <div>
      <div className="aiGeneratingBar" />
      <p className="aiGeneratingText">正在生成，这可能需要几十秒…</p>
      <div className="aiSkeletonList">
        {[1, 2, 3].map((i) => (
          <div key={i} className="aiArticleCard" style={{ background: "var(--cream-2)" }}>
            <div className="aiSkeleton aiSkeletonTitle" />
            <div className="aiSkeleton aiSkeletonBody" />
            <div className="aiSkeleton aiSkeletonBody" style={{ width: "75%" }} />
          </div>
        ))}
      </div>
    </div>
  );
}

function ResultList({
  articles,
  onNavigateToContent,
}: {
  articles: Article[];
  onNavigateToContent: () => void;
}) {
  if (articles.length === 0) {
    return (
      <div className="aiEmptyState">
        <p className="aiEmptyText">生成完成，但未产出文章，请检查 Skill 配置</p>
      </div>
    );
  }
  return (
    <div className="aiArticleList">
      {articles.map((a) => (
        <div key={a.id} className="aiArticleCard">
          <div className="aiArticleTitle">{a.title}</div>
          {a.plain_text && (
            <div className="aiArticleBody">
              {a.plain_text.slice(0, 120)}
              {a.plain_text.length > 120 ? "…" : ""}
            </div>
          )}
          <div className="aiArticleFooter">
            <span className="aiSavedBadge">已保存</span>
            <button
              className="aiOpenLink"
              type="button"
              onClick={onNavigateToContent}
              title="切换到内容管理查看此文章"
            >
              在文章管理中打开
              <ExternalLink size={11} />
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
