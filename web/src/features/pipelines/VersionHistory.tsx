// web/src/features/pipelines/VersionHistory.tsx
import { useEffect, useState } from "react";
import { listVersions, rollbackVersion } from "../../api/pipelines";
import type { PipelineVersionSummary } from "../../types";
import { formatDateTime } from "../../utils/dateFormat";

export function VersionHistory({ pipelineId, onRolledBack }:
  { pipelineId: number; onRolledBack: () => void }) {
  const [rows, setRows] = useState<PipelineVersionSummary[]>([]);
  useEffect(() => { listVersions(pipelineId).then(setRows).catch(() => {}); }, [pipelineId]);
  return (
    <div className="versionHistory">
      <h4 className="versionHistoryTitle">版本历史</h4>
      {rows.map((v) => (
        <div key={v.id} className="versionRow">
          <div className="versionRowInfo">
            <span className="versionNo">v{v.version_no}</span>
            {v.remark ? <span className="versionRemark">{v.remark}</span> : null}
            <span className="versionTime">{formatDateTime(v.created_at)}</span>
          </div>
          <button className="versionRollbackBtn" onClick={async () => {
            if (!window.confirm(`回溯到 v${v.version_no}？将载入草稿，需手动发布后才生效`)) return;
            await rollbackVersion(v.id);
            onRolledBack();
          }}>回溯</button>
        </div>
      ))}
      {rows.length === 0 && <p className="versionEmpty">暂无版本</p>}
    </div>
  );
}
