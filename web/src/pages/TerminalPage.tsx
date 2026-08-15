import { useState } from "react";
import { TerminalSquare } from "lucide-react";
import { TerminalPanel } from "@/components/TerminalPanel";

/**
 * Terminal 页 —— 底部终端面板的展开形态（大视图，复用同一组件 expanded 模式）。
 * 批二十八契约：POST /api/exec + SSE stream；needs_approval 态弹审批卡。
 */
export default function TerminalPage() {
  const [collapsed, setCollapsed] = useState(false);
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-3 flex items-center gap-2">
        <TerminalSquare className="size-5 text-[var(--vigil-muted)]" />
        <h1 className="text-lg font-semibold">Terminal</h1>
        <span className="text-xs text-[var(--vigil-muted)]">
          · 执行 / 审计 / YAML（POST /api/exec + SSE，批二十八契约）
        </span>
      </div>
      <div className="flex min-h-0 flex-1 flex-col">
        <TerminalPanel collapsed={collapsed} onToggle={() => setCollapsed((v) => !v)} expanded />
      </div>
    </div>
  );
}
