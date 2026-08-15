import { ChevronDown, ChevronUp, TerminalSquare } from "lucide-react";
import { ExecConsole } from "@/components/ExecConsole";
import { cn } from "@/lib/ops";

/**
 * Agent Terminal —— 主体下区全宽深色终端面板（豆包：min-h 280px，
 * JetBrains Mono，标题栏 + 收起按钮）。内部为执行控制台
 * （POST /api/exec + SSE；needs_approval 弹审批卡；mock 模式联调）。
 */
export function TerminalPanel({
  collapsed,
  onToggle,
  className,
  expanded = false,
}: {
  collapsed: boolean;
  onToggle: () => void;
  className?: string;
  expanded?: boolean;
}) {
  if (collapsed && !expanded) {
    return (
      <div
        className={cn(
          "flex h-8 shrink-0 items-center gap-2 border-t px-3",
          "border-black/40 font-mono text-xs",
          className,
        )}
        style={{ background: "var(--vigil-terminal-bg)", color: "#94a3b8" }}
      >
        <TerminalSquare className="size-3.5 opacity-70" />
        <span style={{ color: "#cbd5e1" }}>Agent Terminal</span>
        <span className="opacity-60">· 执行 API（POST /api/exec + SSE）</span>
        <button
          type="button"
          onClick={onToggle}
          className="ml-auto inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs opacity-80 hover:bg-white/5"
          aria-label="展开终端面板"
        >
          <ChevronUp className="size-3.5" /> 展开
        </button>
      </div>
    );
  }

  return (
    <div
      className={cn(
        "flex shrink-0 flex-col rounded-lg border border-black/40",
        expanded ? "min-h-0 flex-1" : "min-h-[280px]",
        className,
      )}
      style={{
        background: "var(--vigil-terminal-bg)",
        color: "var(--vigil-terminal-text)",
        boxShadow: "0 1px 3px rgba(0,0,0,0.06)",
      }}
    >
      {/* 标题栏 */}
      <div className="flex h-9 shrink-0 items-center gap-2 border-b border-white/5 px-4 font-mono">
        <TerminalSquare className="size-4 text-sky-400" />
        <span className="text-xs" style={{ color: "#e2e8f0" }}>Agent Terminal</span>
        <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[10px] text-emerald-400">
          <span className="size-1.5 rounded-full bg-emerald-400" />
          API 已接线
        </span>
        <span className="ml-auto text-[10px] opacity-50">JetBrains Mono · POST /api/exec + SSE</span>
        {!expanded && (
          <button
            type="button"
            onClick={onToggle}
            className="inline-flex h-6 items-center rounded px-2 text-xs opacity-70 hover:bg-white/5"
            aria-label="收起终端面板"
          >
            <ChevronDown className="size-3.5" />
          </button>
        )}
      </div>

      {/* 执行控制台 */}
      <div className="min-h-0 flex-1 px-4 py-3">
        <ExecConsole expanded={expanded} />
      </div>
    </div>
  );
}
