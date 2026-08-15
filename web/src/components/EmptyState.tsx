import type { ReactNode } from "react";
import { cn } from "@/lib/ops";

/** 空态占位（不造假数据——数据源未就绪的区块统一用它）。 */
export function EmptyState({
  icon,
  title,
  description,
  hint,
  action,
  className,
}: {
  icon?: ReactNode;
  title: string;
  description?: string;
  hint?: string;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-2 rounded-md border border-dashed px-6 py-10 text-center",
        "border-[var(--vigil-border)]",
        className,
      )}
    >
      <div className="text-[var(--vigil-muted)] opacity-60">{icon}</div>
      <div className="text-sm font-medium text-[var(--vigil-text)] opacity-80">{title}</div>
      {description && (
        <div className="max-w-md text-xs text-[var(--vigil-muted)]">{description}</div>
      )}
      {hint && <div className="text-xs text-[var(--vigil-muted)] opacity-70">{hint}</div>}
      {action && <div className="mt-1">{action}</div>}
    </div>
  );
}
