import { type ReactNode } from "react";

/** 递归 key-value 渲染（已脱敏数据；React 转义是最后一道 XSS 边界）。 */
function renderValue(value: unknown): ReactNode {
  if (value === null || value === undefined) {
    return <span className="opacity-50">-</span>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="opacity-50">[]</span>;
    return (
      <span className="flex flex-wrap gap-x-2 gap-y-1">
        {value.map((v, i) => (
          <span key={i} className="min-w-0 max-w-full">
            {renderValue(v)}
          </span>
        ))}
      </span>
    );
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return <span className="opacity-50">{`{}`}</span>;
    return (
      <div className="w-full">
        {entries.map(([k, v]) => (
          <div
            key={k}
            className="flex gap-2 border-b border-dotted border-[var(--vigil-border)] py-1 last:border-b-0"
          >
            <span className="min-w-32 shrink-0 text-[var(--vigil-muted)]">{k}</span>
            <span className="min-w-0 flex-1 overflow-x-auto">{renderValue(v)}</span>
          </div>
        ))}
      </div>
    );
  }
  // 字符串（命令/YAML 等）：不折行，超出横向滚动——长命令可读。
  return (
    <span className="block whitespace-pre font-mono text-[var(--vigil-text)] opacity-85">
      {String(value)}
    </span>
  );
}

export function DetailTree({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data);
  if (entries.length === 0) {
    return <div className="text-sm text-[var(--vigil-muted)]">无详情数据</div>;
  }
  return (
    <div className="text-sm">
      {entries.map(([k, v]) => (
        <div
          key={k}
          className="flex gap-2 border-b border-dotted border-[var(--vigil-border)] py-1.5 last:border-b-0"
        >
          <span className="min-w-32 shrink-0 text-[var(--vigil-muted)]">{k}</span>
          <span className="min-w-0 flex-1 overflow-x-auto">{renderValue(v)}</span>
        </div>
      ))}
    </div>
  );
}