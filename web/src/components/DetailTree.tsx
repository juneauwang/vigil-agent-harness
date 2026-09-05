import { type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import "@/i18n";

/** Recursive key-value rendering (data already sanitized; React escaping is the last XSS boundary). */
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
  // Strings (commands/YAML etc.): no wrapping, horizontal scroll on overflow — keeps long commands readable.
  return (
    <span className="block whitespace-pre font-mono text-[var(--vigil-text)] opacity-85">
      {String(value)}
    </span>
  );
}

export function DetailTree({ data }: { data: Record<string, unknown> }) {
  const { t } = useTranslation();
  const entries = Object.entries(data);
  if (entries.length === 0) {
    return <div className="text-sm text-[var(--vigil-muted)]">{t("common.noDetailData")}</div>;
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