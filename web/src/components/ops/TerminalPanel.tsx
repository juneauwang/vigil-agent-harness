import { ChevronDown, ChevronUp, Radio, TerminalSquare } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { cn } from "@/lib/utils";

/**
 * Persistent dark terminal panel (方向 1：主体下区常驻，等宽字体).
 *
 * Data source is not wired yet (Codex owns the execution/session core) — the
 * body renders an explicit empty-state placeholder and reserves the
 * scrollable surface + WS-subscription seam for batch 3. No fake log lines
 * are ever rendered.
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
          "flex h-9 shrink-0 items-center gap-2 border-t border-black/40 px-3",
          "bg-[#0b0f14] font-mono text-xs text-slate-400",
          className,
        )}
      >
        <TerminalSquare className="size-3.5" />
        <span className="text-slate-300">执行日志面板</span>
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
        "flex shrink-0 flex-col border-t border-black/50 bg-[#0b0f14]",
        className,
      )}
    >
      {/* Header */}
      <div className="flex h-9 shrink-0 items-center gap-2 border-b border-white/5 px-3">
        <TerminalSquare className="size-3.5 text-teal-400" />
        <span className="font-mono text-xs text-slate-200">执行日志 / 终端</span>
        <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/10 px-2 py-0.5 font-mono text-[10px] text-emerald-400">
          <span className="size-1.5 rounded-full bg-emerald-400" />
          WS 待接入
        </span>
        <span className="ml-auto font-mono text-[10px] text-slate-500">
          JetBrains Mono · 审计 / 命令 / YAML
        </span>
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

      {/* Body — scrollable surface for future log events */}
      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2 font-mono text-xs leading-relaxed text-slate-400">
        <div className="flex h-full min-h-[120px] flex-col items-start justify-center gap-1.5">
          <div className="flex items-center gap-2 text-slate-300">
            <Radio className="size-3.5 text-slate-500" />
            <span>等待执行 API 接入</span>
          </div>
          <p className="text-slate-500">
            审计日志 / AI 执行命令 / YAML 片段将通过 WebSocket 推送到此面板（web_server 已有 WS 基础设施，执行核心由 Codex 批次落地）。
          </p>
        </div>
      </div>
    </div>
  );
}
