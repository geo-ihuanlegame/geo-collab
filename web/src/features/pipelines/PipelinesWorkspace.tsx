import { useCallback, useEffect, useState } from "react";
import { createPipeline, deletePipeline, listPipelines } from "../../api/pipelines";
import { useToast } from "../../components/Toast";
import type { Pipeline } from "../../types";
import { PipelineEditor } from "./PipelineEditor";

export function PipelinesWorkspace() {
  const { toast } = useToast();
  const [items, setItems] = useState<Pipeline[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const reload = useCallback(async () => {
    try {
      const list = await listPipelines();
      setItems(list);
      if (selectedId == null && list.length) setSelectedId(list[0].id);
    } catch (e) {
      toast(e instanceof Error ? e.message : "加载失败", "error");
    }
  }, [selectedId, toast]);

  useEffect(() => { reload(); }, [reload]);

  const onCreate = async () => {
    const name = window.prompt("工作流名称");
    if (!name) return;
    try {
      const p = await createPipeline({ name });
      await reload();
      setSelectedId(p.id);
    } catch (e) {
      toast(e instanceof Error ? e.message : "操作失败", "error");
    }
  };

  const onDelete = async (id: number) => {
    if (!window.confirm("确认删除该工作流？")) return;
    try {
      await deletePipeline(id);
      if (selectedId === id) setSelectedId(null);
      reload();
    } catch (e) {
      toast(e instanceof Error ? e.message : "操作失败", "error");
    }
  };

  return (
    <div className="pipelinesWorkspace">
      <div className="topbar"><div><p className="eyebrow">编排</p><h1>工作流编排</h1></div>
        <button onClick={onCreate}>+ 新建工作流</button></div>
      <div style={{ display: "flex", gap: 16 }}>
        <aside style={{ width: 220 }}>
          {items.map((p) => (
            <div key={p.id} onClick={() => setSelectedId(p.id)}
                 style={{ fontWeight: p.id === selectedId ? 700 : 400, cursor: "pointer", padding: 6 }}>
              {p.name}{p.has_draft ? " ●" : ""}
              <button style={{ float: "right" }} onClick={(e) => { e.stopPropagation(); onDelete(p.id); }}>删</button>
            </div>
          ))}
        </aside>
        <main style={{ flex: 1 }}>
          {selectedId != null
            ? <PipelineEditor pipelineId={selectedId} onChanged={reload} />
            : <p>请选择或新建工作流</p>}
        </main>
      </div>
    </div>
  );
}
