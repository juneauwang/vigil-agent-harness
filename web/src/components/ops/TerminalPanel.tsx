import { ChevronDown, ChevronUp, Radio, TerminalSquare } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { cn } from "@/lib/utils";

/**
 * Agent Terminal —— 主体下区全宽深色终端面板（豆包布局：min-h 280px，
 * JetBrains Mono，标题栏 + 收起按钮）。
 *
 * 数据源未就绪（执行/审计日志来自 Codex 核心批次）：正文为显式空态占位 +
 * WS 订阅预留，绝不造假日志。展开时最小高度 280px，收起后只剩一条细栏。
 */
export function TerminalPanel({
  collapsed,
  onToggle,
  className,
}: {
  collapsed: boolean;
  onToggle: () => void;
  className?: string;
}) {
  if (collapsed) {
    return (
      <div
        className={cn(
          "flex h-8 shrink-0 items-center gap-2 border-t border-black/40 px-3",
          "bg-[#0f1c2d] font-mono text-xs text-slate-400",
          className,
        )}
      >
        <TerminalSquare className="size-3.5 text-slate-500" />
        <span className="text-slate-300">Agent Terminal</span>
        <span className="text-slate-600">· 等待执行 API 接入</span>
        <Button
          ghost
          size="sm"
          onClick={onToggle}
          className="ml-auto h-6 px-2 text-slate-400 hover:bg-white/5 hover:text-slate-200"
          aria-label="展开终端面板"
        >
          <ChevronUp className="size-3.5" /> 展开
        </Button>
      </div>
    );
  }

  return (
    <div
      className={cn(
        "flex shrink-0 flex-col rounded-lg border border-black/40 bg-[#0f1c2d] font-mono shadow-[0_1px_3px_rgba(0,0,0,0.06)]",
        className,
      )}
      style={{ minHeight: "280px" }}
    >
      {/* 标题栏 */}
      <div className="flex h-9 shrink-0 items-center gap-2 border-b border-white/5 px-4">
        <TerminalSquare className="size-4 text-sky-400" />
        <span className="text-xs text-slate-200">Agent Terminal</span>
        <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[10px] text-emerald-400">
          <span className="size-1.5 rounded-full bg-emerald-400" />
          WS 待接入
        </span>
        <span className="ml-auto text-[10px] text-slate-500">JetBrains Mono · 审计 / 命令 / YAML</span>
        <Button
          ghost
          size="sm"
          onClick={onToggle}
          className="h-6 px-2 text-slate-400 hover:bg-white/5 hover:text-slate-200"
          aria-label="收起终端面板"
        >
          <ChevronDown className="size-3.5" />
        </Button>
      </div>

      {/* 终端正文（滚动区，空态 + WS 预留） */}
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3 text-xs leading-relaxed text-slate-400">
        <div className="flex h-full min-h-[240px] flex-col items-start justify-center gap-1.5">
          <div className="flex items-center gap-2 text-slate-300">
            <Radio className="size-3.5 text-slate-500" />
            <span>等待执行 API 接入</span>
          </div>
          <p className="text-slate-500">
            审计日志 / AI 执行命令 / YAML 片段将经 WebSocket 推送到此面板（web_server
            已有 WS 基础设施，执行核心由 Codex 批次落地）；不造假日志。
          </p>
        </div>
      </div>
    </div>
  );
}
