import { GenerateTab } from "./GenerateTab";

export function AiGenerationWorkspace({ onNavigateToContent }: { onNavigateToContent: () => void }) {
  return (
    <div className="aiWorkspace">
      <div className="topbar" style={{ marginBottom: 0 }}>
        <div>
          <p className="eyebrow">AI 生文</p>
          <h1>智能创作</h1>
        </div>
      </div>

      <div className="aiTabContent">
        <GenerateTab onNavigateToContent={onNavigateToContent} />
      </div>
    </div>
  );
}
