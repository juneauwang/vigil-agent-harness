import { Radio, TerminalSquare } from "lucide-react";
import { EmptyState } from "@/components/ops/EmptyState";

/**
 * 执行日志页 —— 常驻终端面板的展开形态（大视图）。
 *
 * 数据源未就绪（AI 执行日志来自会话/执行 API，Codex 正在写核心）：本页
 * 展示布局 + 空态 + WS 订阅预留，绝不造假日志。
 */
export default function ExecutionLogsPage() {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-3 flex items-center gap-2">
        <TerminalSquare className="size-5 text-muted-foreground" />
        <h1 className="text-lg font-semibold">执行日志</h1>
        <span className="text-xs text-muted-foreground">
          · 审计日志 / AI 执行命令 / YAML 片段
        </span>
      </div>

      <div className="flex min-h-0 flex-1 flex-col rounded-md border border-black/40 bg-[#0b0f14]">
        {/* Fake terminal chrome */}
        <div className="flex h-8 shrink-0 items-center gap-1.5 border-b border-white/5 px-3">
          <span className="size-2 rounded-full bg-red-500/60" />
          <span className="size-2 rounded-full bg-amber-500/60" />
          <span className="size-2 rounded-full bg-emerald-500/60" />
          <span className="ml-2 font-mono text-[11px] text-slate-500">
            vigil-exec-logs — ws://127.0.0.1:9119/api/ws（预留）
          </span>
        </div>

        {/* Terminal body */}
        <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 px-6 font-mono text-xs text-slate-400">
          <div className="flex items-center gap-2 text-slate-300">
            <Radio className="size-4 text-slate-500" />
            <span>等待执行 API 接入</span>
          </div>
          <p className="max-w-lg text-center leading-relaxed text-slate-500">
            执行日志来自 AIAgent 会话/执行核心（Codex 批次落地）。接入后，审计日志与
            执行命令将经 WebSocket 实时推送到此大视图与主体下区常驻终端面板；
            本页为展开形态，面板为常驻折叠形态，共用同一事件流。
          </p>
          <EmptyState
            title="无日志事件"
            description="当前无执行事件；不造假数据，接入后此处滚动显示真实日志。"
            className="mt-2 max-w-md border-slate-700 py-8"
          />
        </div>
      </div>
    </div>
  );
}
