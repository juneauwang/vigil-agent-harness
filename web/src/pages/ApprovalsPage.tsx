import { ShieldCheck } from "lucide-react";
import { EmptyState } from "@/components/EmptyState";

/** Approvals —— 审批 API 未就绪（Codex 核心批次落地），空态占位。 */
export default function ApprovalsPage() {
  return (
    <div className="mx-auto w-full max-w-5xl">
      <div className="mb-4 flex items-center gap-2">
        <ShieldCheck className="size-5 text-[var(--vigil-muted)]" />
        <h1 className="text-lg font-semibold">Approvals</h1>
        <span className="rounded-full bg-[var(--vigil-muted-bg)] px-2 py-0.5 text-xs text-[var(--vigil-muted)]">
          0 待审批
        </span>
      </div>
      <EmptyState
        icon={<ShieldCheck className="size-6" />}
        title="审批 API 未就绪"
        description="高风险运维变更（prod 变更类 / 提权命令 / 凭据访问）的审批流由 Codex 核心批次落地。"
        hint="规划交互：风险分级列表（高/中/低）+ 滑出详情面板 + 批准/拒绝操作"
        className="py-16"
      />
    </div>
  );
}
