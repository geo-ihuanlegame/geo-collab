// web/src/features/pipelines/VersionHistory.tsx
import { useEffect, useState } from "react";
import { listVersions, rollbackVersion } from "../../api/pipelines";
import type { PipelineVersionSummary } from "../../types";

export function VersionHistory({ pipelineId, onRolledBack }:
  { pipelineId: number; onRolledBack: () => void }) {
  const [rows, setRows] = useState<PipelineVersionSummary[]>([]);
  useEffect(() => { listVersions(pipelineId).then(setRows).catch(() => {}); }, [pipelineId]);
  return (
    <div>
      <h4>版本历史</h4>
      {rows.map((v) => (
        <div key={v.id}>
          v{v.version_no} {v.remark ?? ""} {new Date(v.created_at).toLocaleString()}
          <button onClick={async () => {
            if (!window.confirm(`回溯到 v${v.version_no}？将载入草稿，需手动发布后才生效`)) return;
            await rollbackVersion(v.id);
            onRolledBack();
          }}>回溯</button>
        </div>
      ))}
      {rows.length === 0 && <p>暂无版本</p>}
    </div>
  );
}
