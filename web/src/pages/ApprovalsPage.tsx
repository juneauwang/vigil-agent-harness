import { ShieldCheck } from "lucide-react";
import { EmptyState } from "@/components/ops/EmptyState";

/**
 * 审批中心 —— 占位页（第三批接入审批 API 后填充）。
 *
 * 交互设计参考 DeepSeek 方案：风险分级列表 + 滑出详情面板；本批只搭导航与
 * 空态，顶部栏待审批红点角标数量 0。
 */
export default function ApprovalsPage() {
  return (
    <div className="mx-auto w-full max-w-5xl p-4 lg:p-6">
      <div className="mb-4 flex items-center gap-2">
        <ShieldCheck className="size-5 text-muted-foreground" />
        <h1 className="text-lg font-semibold">审批中心</h1>
        <span className="rounded-full bg-slate-500/15 px-2 py-0.5 text-xs text-muted-foreground">
          0 待审批
        </span>
      </div>

      <EmptyState
        icon={<ShieldCheck className="size-6" />}
        title="审批 API 未就绪"
        description="高风险运维变更（prod 变更类 / 提权命令 / 凭据访问）的审批流由 Codex 核心批次落地，第三批在此填充。"
        hint="规划交互：风险分级列表（高/中/低）+ 滑出详情面板 + 批准/拒绝操作"
        className="py-16"
      />
    </div>
  );
}
