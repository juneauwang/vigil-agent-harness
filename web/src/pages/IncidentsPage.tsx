import { useEffect, useState } from "react";
import { CheckCircle2, TriangleAlert } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import type { IncidentItem } from "@/lib/api";
import { isMockEnabled } from "@/lib/mock";
import { EmptyState } from "@/components/EmptyState";
import { cn } from "@/lib/ops";

/**
 * Incidents 告警页（批五十）：/api/incidents 读 watch inbox 返回告警列表。
 * severity 分级样式：critical=红 / warning=黄 / info=蓝；空 inbox → "暂无告警"。
 * processed 只读展示（标记处理是 watch_digest agent 通道的事，本页不做）。
 */

function severityBadge(severity?: string) {
  const s = (severity ?? "").toLowerCase();
  if (s === "critical") {
    return { label: "critical", cls: "bg-[var(--vigil-error)] text-white" };
  }
  if (s === "warning") {
    return { label: "warning", cls: "bg-[var(--vigil-warn)] text-[var(--vigil-dark)]" };
  }
  return { label: s || "info", cls: "bg-[var(--vigil-primary)] text-white" };
}

function IncidentRow({ incident }: { incident: IncidentItem }) {
  const badge = severityBadge(incident.severity);
  return (
    <div className="rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)] p-3">
      <div className="flex items-start gap-3">
        <span
          className={cn(
            "mt-0.5 inline-flex shrink-0 items-center rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
            badge.cls,
          )}
        >
          {badge.label}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium text-[var(--vigil-text)]">
              {incident.alertname || "未知告警"}
            </span>
            {incident.processed && (
              <span className="inline-flex shrink-0 items-center gap-1 text-[10px] text-[var(--vigil-ok)]">
                <CheckCircle2 className="size-3" /> 已处理
              </span>
            )}
          </div>
          <div className="mt-1.5 grid grid-cols-1 gap-x-6 gap-y-1 text-xs text-[var(--vigil-muted)] sm:grid-cols-2">
            <div className="truncate">
              <span className="text-[var(--vigil-muted)]">instance：</span>
              <span className="text-[var(--vigil-text)]">{incident.instance || "-"}</span>
            </div>
            <div className="truncate">
              <span className="text-[var(--vigil-muted)]">state：</span>
              <span className="text-[var(--vigil-text)]">{incident.state || "-"}</span>
            </div>
            <div className="truncate">
              <span className="text-[var(--vigil-muted)]">startsAt：</span>
              <span className="text-[var(--vigil-text)]">{incident.startsAt || "-"}</span>
            </div>
            <div className="truncate">
              <span className="text-[var(--vigil-muted)]">collected_at：</span>
              <span className="text-[var(--vigil-text)]">{incident.collected_at || "-"}</span>
            </div>
            <div className="truncate">
              <span className="text-[var(--vigil-muted)]">source：</span>
              <span className="text-[var(--vigil-text)]">{incident.source || "-"}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function IncidentsPage() {
  const mock = isMockEnabled();
  const [items, setItems] = useState<IncidentItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    if (mock) {
      setLoaded(true);
      return;
    }
    api
      .getIncidents({ limit: 50 })
      .then((resp) => {
        if (!alive) return;
        if (resp.error) {
          setError(resp.error.message ?? "加载失败");
        } else {
          setItems(resp.incidents ?? []);
          setTotal(resp.total ?? 0);
        }
        setLoaded(true);
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setError(
          e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e),
        );
        setLoaded(true);
      });
    return () => {
      alive = false;
    };
  }, [mock]);

  return (
    <div className="mx-auto w-full max-w-5xl">
      <div className="mb-4 flex items-center gap-2">
        <TriangleAlert className="size-5 text-[var(--vigil-muted)]" />
        <h1 className="text-lg font-semibold">Incidents</h1>
        <span className="text-xs text-[var(--vigil-muted)]">· 故障与告警</span>
        {loaded && !error && total > 0 && (
          <span className="ml-auto rounded bg-[var(--vigil-muted-bg)] px-2 py-0.5 text-xs text-[var(--vigil-muted)]">
            {total} 条
          </span>
        )}
      </div>

      {!loaded ? null : error ? (
        <EmptyState
          icon={<TriangleAlert className="size-6" />}
          title="加载失败"
          description={error}
          hint="请确认 dashboard 后端已启动且 watch 采集层可用。"
          className="py-16"
        />
      ) : items.length === 0 ? (
        <EmptyState
          icon={<TriangleAlert className="size-6" />}
          title="暂无告警"
          description="watch 采集层（ops.watch）接入 Alertmanager 后，活跃告警会出现在这里。"
          hint="inbox 位于 ~/.vigil/watch/inbox/；无告警时采集层不写 inbox。"
          className="py-16"
        />
      ) : (
        <div className="space-y-2">
          {items.map((inc, idx) => (
            <IncidentRow
              key={`${inc.alertname ?? "?"}|${inc.instance ?? "?"}|${inc.startsAt ?? idx}`}
              incident={inc}
            />
          ))}
        </div>
      )}
    </div>
  );
}
