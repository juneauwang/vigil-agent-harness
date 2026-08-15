import { History } from "lucide-react";
import { EmptyState } from "@/components/EmptyState";

/** Audit —— 无数据源（审计日志来自执行/会话核心），空态占位 + WS 预留。 */
export default function AuditPage() {
  return (
    <div className="mx-auto w-full max-w-5xl">
      <div className="mb-4 flex items-center gap-2">
        <History className="size-5 text-[var(--vigil-muted)]" />
        <h1 className="text-lg font-semibold">Audit</h1>
        <span className="text-xs text-[var(--vigil-muted)]">· 审计日志</span>
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
