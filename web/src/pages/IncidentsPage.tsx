import { TriangleAlert } from "lucide-react";
import { EmptyState } from "@/components/ops/EmptyState";

/**
 * Incidents 页 —— 无数据源（事件/告警 API 由 Codex 核心批次落地）。
 * 空态占位，不造假数据。
 */
export default function IncidentsPage() {
  return (
    <div className="mx-auto w-full max-w-5xl p-4 lg:p-6">
      <div className="mb-4 flex items-center gap-2">
        <TriangleAlert className="size-5 text-muted-foreground" />
        <h1 className="text-lg font-semibold">Incidents</h1>
        <span className="text-xs text-muted-foreground">· 事件与告警</span>
      </div>
      <EmptyState
        icon={<TriangleAlert className="size-6" />}
        title="暂无 Incident 数据"
        description="事件/告警流来自执行与监控 API（Codex 批次落地），接入后按严重度分级展示。"
        hint="本批为占位，不造假数据"
        className="py-16"
      />
    </div>
  );
}
