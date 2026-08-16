import { Square } from "lucide-react";

/**
 * 对话页"停止"按钮（批三十六）：agent busy 时显示，点击走后端真中断。
 * 警示样式（红色系，与"发送"并列）；stopping 时禁用并显示"停止中…"。
 * 抽成独立组件便于 node 环境 SSR 单测（按钮显隐）。
 */
export default function StopButton({
  stopping,
  onStop,
}: {
  stopping: boolean;
  onStop: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onStop}
      disabled={stopping}
      aria-label="停止"
      title="中断当前操作（后端真中断，不再浪费 token）"
      className="vigil-btn h-8 shrink-0 whitespace-nowrap border border-red-500/50 px-3 text-sm text-red-600 hover:bg-red-500/10 disabled:opacity-50 dark:text-red-400"
    >
      <Square className="size-3.5" /> {stopping ? "停止中…" : "停止"}
    </button>
  );
}
