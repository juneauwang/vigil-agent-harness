import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * Recursive key-value renderer for the ops dashboard detail panels.
 *
 * Renders arbitrary sanitized JSON (topology layer-3 archives / runbook
 * detail) as a compact definition list. Values are rendered with React
 * (auto-escaped) — the server has already credential-redacted them, and
 * React's JSX escaping is the final XSS boundary.
 */
function renderValue(value: unknown): ReactNode {
  if (value === null || value === undefined) {
    return <span className="text-muted-foreground/60">-</span>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return <span className="text-muted-foreground/60">[]</span>;
    }
    return (
      <span className="flex flex-wrap gap-x-2 gap-y-1">
        {value.map((v, i) => (
          <span key={i} className="break-all">
            {renderValue(v)}
          </span>
        ))}
      </span>
    );
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) {
      return <span className="text-muted-foreground/60">{`{}`}</span>;
    }
    return (
      <div className="w-full">
        {entries.map(([k, v]) => (
          <div
            key={k}
            className="flex gap-2 border-b border-dotted border-border/60 py-1 last:border-b-0"
          >
            <span className="min-w-32 shrink-0 text-muted-foreground">{k}</span>
            <span className="break-all">{renderValue(v)}</span>
          </div>
        ))}
      </div>
    );
  }
  return <span className="break-all">{String(value)}</span>;
}

export function OpsKeyValueTree({
  data,
  className,
}: {
  data: Record<string, unknown>;
  className?: string;
}) {
  const entries = Object.entries(data);
  if (entries.length === 0) {
    return <div className={cn("text-muted-foreground", className)}>无详情数据</div>;
  }
  return (
    <div className={cn("text-sm", className)}>
      {entries.map(([k, v]) => (
        <div key={k} className="flex gap-2 border-b border-dotted border-border/60 py-1.5 last:border-b-0">
          <span className="min-w-32 shrink-0 text-muted-foreground">{k}</span>
          <span className="min-w-0 flex-1">{renderValue(v)}</span>
        </div>
      ))}
    </div>
  );
}
