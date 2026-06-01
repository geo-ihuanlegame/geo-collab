import { useState } from "react";
import { GenerateTab } from "./GenerateTab";
import { SkillsPromptsTab } from "./SkillsPromptsTab";

type Tab = "generate" | "library";

export function AiGenerationWorkspace({ onNavigateToContent }: { onNavigateToContent: () => void }) {
  const [tab, setTab] = useState<Tab>("generate");

  return (
    <div className="aiWorkspace">
      <div className="topbar" style={{ marginBottom: 0 }}>
        <div>
          <p className="eyebrow">AI 生文</p>
          <h1>智能创作</h1>
        </div>
      </div>

      <div className="aiTabs">
        <button
          className={`aiTabBtn${tab === "generate" ? " active" : ""}`}
          type="button"
          onClick={() => setTab("generate")}
        >
          一键生成
        </button>
        <button
          className={`aiTabBtn${tab === "library" ? " active" : ""}`}
          type="button"
          onClick={() => setTab("library")}
        >
          技能与提示词
        </button>
      </div>

      <div className="aiTabContent">
        {tab === "generate" && (
          <GenerateTab onNavigateToContent={onNavigateToContent} />
        )}
        {tab === "library" && <SkillsPromptsTab />}
      </div>
    </div>
  );
}
