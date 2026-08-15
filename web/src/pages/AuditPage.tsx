import { History } from "lucide-react";
import { EmptyState } from "@/components/ops/EmptyState";

/**
 * Audit 页 —— 无数据源（审计日志来自执行/会话核心，Codex 批次落地）。
 * 空态占位，不造假数据；与底部 Agent Terminal 共用同一事件流（WS 预留）。
 */
export default function AuditPage() {
  return (
    <div className="mx-auto w-full max-w-5xl p-4 lg:p-6">
      <div className="mb-4 flex items-center gap-2">
        <History className="size-5 text-muted-foreground" />
        <h1 className="text-lg font-semibold">Audit</h1>
        <span className="text-xs text-muted-foreground">· 审计日志</span>
      </div>
      <EmptyState
        icon={<History className="size-6" />}
        title="暂无审计记录"
        description="审计日志（谁在什么时候执行了什么命令）来自执行核心，接入后经 WebSocket 推送至此页与底部终端面板。"
        hint="本批为占位，不造假数据"
        className="py-16"
      />
    </div>
  );
}
