import type { ReactNode } from "react";
import { Inbox } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Compact empty-state placeholder for ops pages whose data source isn't
 * wired yet (execution logs / approvals / runbook executions). No fake
 * data is ever rendered — these blocks exist so the layout is visible
 * before the corresponding API lands (batch 3).
 */
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
        "flex flex-col items-center justify-center gap-2 rounded-md border border-dashed border-border px-6 py-10 text-center",
        className,
      )}
    >
      <div className="text-muted-foreground/60">
        {icon ?? <Inbox className="size-6" />}
      </div>
      <div className="text-sm font-medium text-foreground/80">{title}</div>
      {description && (
        <div className="max-w-md text-xs text-muted-foreground">{description}</div>
      )}
      {hint && <div className="text-xs text-muted-foreground/70">{hint}</div>}
      {action && <div className="mt-1">{action}</div>}
    </div>
  );
}
