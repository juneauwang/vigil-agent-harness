import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Activity, AlertTriangle, Play, RefreshCw, Search, Server } from "lucide-react";
import { ApiError, api } from "@/lib/api";
import type {
  MonitoringAlert,
  MonitoringHealthService,
  MonitoringSeries,
} from "@/lib/api";
import { EmptyState } from "@/components/EmptyState";
import { cn } from "@/lib/ops";

/**
 * 监控页（OPS-DELTA #78）：服务健康 / PromQL 查询 / 活跃告警。
 *
 * 三块布局（Grafana 心智 + 现有蓝灰主题）：
 * 1. 服务健康列表——拓扑服务按需探测（后端 30s 缓存），up 绿 / down 红 /
 *    unknown 灰；顶部汇总 + 状态筛选 + 30s 自动刷新。
 * 2. PromQL 查询——promql + duration/step → 结构化 series 表格 + SVG
 *    sparkline（手写轻量，不引图表库）；未配置 Prometheus 时显示引导。
 * 3. 活跃告警——Alertmanager 实时快照，severity 色标复用 Incidents 分级样式。
 * 无监控数据/全 unknown 不崩（空态 + 引导文案）。
 */

const STATUS_META: Record<string, { label: string; cls: string }> = {
  up: { label: "up", cls: "bg-[var(--vigil-ok)] text-white" },
  down: { label: "down", cls: "bg-[var(--vigil-error)] text-white" },
  unknown: { label: "unknown", cls: "bg-[var(--vigil-muted)] text-white" },
};

const REFRESH_MS = 30_000;

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

function errText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  return e instanceof Error ? e.message : String(e);
}

/** 手写 SVG sparkline（不引图表库）：series 时间点折线，单值/全空返回 null。 */
function Sparkline({ points, width = 132, height = 28 }: {
  points: Array<[number, number | null]>;
  width?: number;
  height?: number;
}) {
  const line = useMemo(() => {
    const valid = points.filter(([, v]) => typeof v === "number" && v === v);
    if (valid.length < 2) return null;
    const values = valid.map(([, v]) => v as number);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const step = width / (valid.length - 1);
    return valid.map(([, v], i) => {
      const x = i * step;
      const y = height - 2 - ((v as number - min) / span) * (height - 4);
      return { x, y };
    });
  }, [points, width, height]);
  if (!line) return null;
  return (
    <svg
      width={width}
      height={height}
      className="shrink-0 overflow-visible"
      aria-label="sparkline"
      role="img"
    >
      <polyline
        points={line.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ")}
        fill="none"
        stroke="var(--vigil-primary)"
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}

function fmtNum(v?: number): string {
  if (typeof v !== "number") return "-";
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

function SeriesRow({ series }: { series: MonitoringSeries }) {
  const labelText = Object.entries(series.labels ?? {})
    .map(([k, v]) => `${k}=${v}`)
    .join(" ");
  return (
    <div className="flex items-center gap-3 rounded border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="truncate text-xs font-medium text-[var(--vigil-text)]">
          {series.name || "{no name}"}
          {series.labels && Object.keys(series.labels).length > 0 && (
            <span className="text-[var(--vigil-muted)]"> {labelText}</span>
          )}
        </div>
        <div className="mt-0.5 text-[10px] text-[var(--vigil-muted)]">
          {series.point_count} 点 · last {fmtNum(series.summary.last)} · min{" "}
          {fmtNum(series.summary.min)} · max {fmtNum(series.summary.max)}
        </div>
      </div>
      <Sparkline points={series.points} />
    </div>
  );
}

function AlertRow({ alert }: { alert: MonitoringAlert }) {
  const badge = severityBadge(alert.severity);
  return (
    <div className="flex items-start gap-3 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)] p-3">
      <span
        className={cn(
          "mt-0.5 inline-flex shrink-0 items-center rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
          badge.cls,
        )}
      >
        {badge.label}
      </span>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium text-[var(--vigil-text)]">
          {alert.alertname || "未知告警"}
        </div>
        <div className="mt-0.5 truncate text-[11px] text-[var(--vigil-muted)]">
          {[alert.instance, alert.startsAt && new Date(alert.startsAt).toLocaleString()]
            .filter(Boolean)
            .join(" · ") || "—"}
        </div>
      </div>
    </div>
  );
}

type StatusFilter = "all" | "up" | "down" | "unknown";

export default function MonitoringPage() {
  const [health, setHealth] = useState<MonitoringHealthService[]>([]);
  const [summary, setSummary] = useState({ up: 0, down: 0, unknown: 0 });
  const [healthError, setHealthError] = useState<string | null>(null);
  const [healthLoaded, setHealthLoaded] = useState(false);
  const [filter, setFilter] = useState<StatusFilter>("all");

  const [promql, setPromql] = useState("");
  const [duration, setDuration] = useState("");
  const [step, setStep] = useState("");
  const [querySeries, setQuerySeries] = useState<MonitoringSeries[]>([]);
  const [queryMeta, setQueryMeta] = useState<{ query: string; duration: string; step: string } | null>(null);
  const [queryError, setQueryError] = useState<string | null>(null);
  const [querying, setQuerying] = useState(false);
  const [queryDone, setQueryDone] = useState(false);

  const [alerts, setAlerts] = useState<MonitoringAlert[]>([]);
  const [alertsError, setAlertsError] = useState<string | null>(null);
  const [alertsLoaded, setAlertsLoaded] = useState(false);

  const loadHealth = useCallback((refresh: boolean) => {
    api
      .getMonitoringHealth(refresh)
      .then((resp) => {
        setHealth(resp.data?.services ?? []);
        setSummary(
          resp.data?.summary ?? { up: 0, down: 0, unknown: 0 },
        );
        setHealthError(null);
        setHealthLoaded(true);
      })
      .catch((e: unknown) => {
        setHealthError(errText(e));
        setHealthLoaded(true);
      });
  }, []);

  const loadAlerts = useCallback(() => {
    api
      .getMonitoringAlerts()
      .then((resp) => {
        setAlerts(resp.data?.alerts ?? []);
        setAlertsError(null);
        setAlertsLoaded(true);
      })
      .catch((e: unknown) => {
        setAlertsError(errText(e));
        setAlertsLoaded(true);
      });
  }, []);

  useEffect(() => {
    loadHealth(false);
    loadAlerts();
    const timer = setInterval(() => loadHealth(false), REFRESH_MS);
    return () => clearInterval(timer);
  }, [loadHealth, loadAlerts]);

  const runQuery = (e: FormEvent) => {
    e.preventDefault();
    const q = promql.trim();
    if (!q || querying) return;
    setQuerying(true);
    setQueryDone(false);
    setQueryError(null);
    api
      .queryMonitoring(q, duration.trim() || undefined, step.trim() || undefined)
      .then((resp) => {
        setQuerySeries(resp.data?.series ?? []);
        setQueryMeta(
          resp.data
            ? { query: resp.data.query, duration: resp.data.duration, step: resp.data.step }
            : null,
        );
        setQueryDone(true);
      })
      .catch((e: unknown) => {
        setQuerySeries([]);
        setQueryMeta(null);
        setQueryError(errText(e));
        setQueryDone(true);
      })
      .finally(() => setQuerying(false));
  };

  const filtered = useMemo(
    () => (filter === "all" ? health : health.filter((s) => s.status === filter)),
    [health, filter],
  );
  const isPromUnavailable =
    queryError !== null &&
    (queryError.includes("未配置 Prometheus") ||
      queryError.includes("prometheus_unavailable"));

  return (
    <div className="mx-auto w-full max-w-5xl">
      <div className="mb-4 flex items-center gap-2">
        <Activity className="size-5 text-[var(--vigil-muted)]" />
        <h1 className="text-lg font-semibold">Monitoring</h1>
        <span className="text-xs text-[var(--vigil-muted)]">· 服务健康与指标</span>
      </div>

      <div className="space-y-4">
        {/* 1. 服务健康 */}
        <section className="vigil-card p-4">
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <h3 className="text-sm font-medium">服务健康</h3>
            <div className="flex items-center gap-1.5">
              {(["up", "down", "unknown"] as const).map((k) => (
                <button
                  key={k}
                  type="button"
                  onClick={() => setFilter(k)}
                  className={cn(
                    "rounded px-2 py-0.5 text-[11px] font-medium transition-colors",
                    filter === k
                      ? "bg-[var(--vigil-primary)] text-white"
                      : "bg-[var(--vigil-muted-bg)] text-[var(--vigil-muted)] hover:text-[var(--vigil-text)]",
                  )}
                >
                  {k} {summary[k]}
                </button>
              ))}
              <button
                type="button"
                onClick={() => setFilter("all")}
                className={cn(
                  "rounded px-2 py-0.5 text-[11px] font-medium transition-colors",
                  filter === "all"
                    ? "bg-[var(--vigil-primary)] text-white"
                    : "bg-[var(--vigil-muted-bg)] text-[var(--vigil-muted)] hover:text-[var(--vigil-text)]",
                )}
              >
                全部 {health.length}
              </button>
            </div>
            <button
              type="button"
              onClick={() => loadHealth(true)}
              className="ml-auto inline-flex items-center gap-1 rounded border border-[var(--vigil-border)] px-2 py-1 text-[11px] text-[var(--vigil-muted)] hover:text-[var(--vigil-text)]"
            >
              <RefreshCw className="size-3" /> 刷新
            </button>
          </div>

          {!healthLoaded ? null : healthError ? (
            <EmptyState
              icon={<Activity className="size-6" />}
              title="健康数据加载失败"
              description={healthError}
              hint="健康探测不依赖 Prometheus；请确认 dashboard 后端与拓扑数据（services/）可用。"
              className="py-10"
            />
          ) : health.length === 0 ? (
            <EmptyState
              icon={<Server className="size-6" />}
              title="暂无拓扑服务"
              description="拓扑 services/ 目录为空或 topology.yaml 缺失——健康探测无对象。"
              hint="运行 vigil ops-init 生成样例拓扑，或 vigil topo-discover 采集后回来刷新。"
              className="py-10"
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-[var(--vigil-border)] text-[10px] uppercase tracking-wide text-[var(--vigil-muted)]">
                    <th className="py-1.5 pr-2 font-medium">服务</th>
                    <th className="px-2 py-1.5 font-medium">类型</th>
                    <th className="px-2 py-1.5 font-medium">集群</th>
                    <th className="px-2 py-1.5 font-medium">endpoint</th>
                    <th className="px-2 py-1.5 font-medium">状态</th>
                    <th className="px-2 py-1.5 font-medium">延迟</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((s) => {
                    const meta = STATUS_META[s.status] ?? STATUS_META.unknown;
                    return (
                      <tr key={`${s.host ?? ""}|${s.name}`} className="border-b border-[var(--vigil-border)]/60 last:border-0">
                        <td className="py-1.5 pr-2">
                          <div className="font-medium text-[var(--vigil-text)]">{s.name}</div>
                          {s.host && (
                            <div className="text-[10px] text-[var(--vigil-muted)]">{s.host}</div>
                          )}
                        </td>
                        <td className="px-2 py-1.5 text-[var(--vigil-muted)]">{s.type ?? "-"}</td>
                        <td className="px-2 py-1.5 text-[var(--vigil-muted)]">{s.cluster ?? "-"}</td>
                        <td className="max-w-[180px] truncate px-2 py-1.5 font-mono text-[10px] text-[var(--vigil-muted)]">
                          {s.endpoint ?? "—"}
                        </td>
                        <td className="px-2 py-1.5">
                          <span
                            className={cn(
                              "inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
                              meta.cls,
                            )}
                          >
                            {meta.label}
                          </span>
                        </td>
                        <td className="px-2 py-1.5 text-[var(--vigil-muted)]">
                          {typeof s.latency_ms === "number" ? `${s.latency_ms} ms` : "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {/* 2. PromQL 查询 */}
        <section className="vigil-card p-4">
          <h3 className="mb-3 text-sm font-medium">PromQL 查询</h3>
          <form onSubmit={runQuery} className="mb-3 flex flex-wrap items-center gap-2">
            <div className="relative min-w-0 flex-1">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-[var(--vigil-muted)]" />
              <input
                value={promql}
                onChange={(e) => setPromql(e.target.value)}
                placeholder="输入 PromQL，如 up、rate(http_requests_total[5m])"
                className="vigil-input w-full rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)] py-1.5 pl-8 pr-3 text-xs outline-none focus:border-[var(--vigil-primary)]"
              />
            </div>
            <input
              value={duration}
              onChange={(e) => setDuration(e.target.value)}
              placeholder="duration 30m"
              title="查询时长（默认 30m）"
              className="vigil-input w-28 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)] px-2 py-1.5 text-xs outline-none focus:border-[var(--vigil-primary)]"
            />
            <input
              value={step}
              onChange={(e) => setStep(e.target.value)}
              placeholder="step 60s"
              title="采样步长（默认 60s）"
              className="vigil-input w-28 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)] px-2 py-1.5 text-xs outline-none focus:border-[var(--vigil-primary)]"
            />
            <button
              type="submit"
              disabled={querying || !promql.trim()}
              className="inline-flex items-center gap-1 rounded-md bg-[var(--vigil-primary)] px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
            >
              <Play className="size-3" /> {querying ? "查询中…" : "执行"}
            </button>
          </form>

          {isPromUnavailable ? (
            <EmptyState
              icon={<Search className="size-6" />}
              title="Prometheus 未配置"
              description={queryError}
              hint="在 config.yaml 配置 ops.prometheus.endpoint 后，PromQL 查询与告警可用；服务健康不依赖 Prometheus，照常工作。"
              className="py-8"
            />
          ) : queryError ? (
            <EmptyState
              icon={<AlertTriangle className="size-6" />}
              title="查询失败"
              description={queryError}
              hint="可检查 PromQL 语法或上游 Prometheus 状态后重试。"
              className="py-8"
            />
          ) : !queryDone ? (
            <div className="py-6 text-center text-xs text-[var(--vigil-muted)]">
              输入 PromQL 并执行，结果会以 series + sparkline 展示。
            </div>
          ) : querySeries.length === 0 ? (
            <EmptyState
              icon={<Search className="size-6" />}
              title="查询无结果"
              description={`${queryMeta?.query ?? ""} 无匹配 series（${queryMeta?.duration ?? ""}/${queryMeta?.step ?? ""} 范围）。`}
              className="py-8"
            />
          ) : (
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <span className="text-[11px] text-[var(--vigil-muted)]">
                  {querySeries.length} 个 series · {queryMeta?.duration ?? ""}/{queryMeta?.step ?? ""}
                </span>
                {queryMeta && <span className="truncate font-mono text-[11px] text-[var(--vigil-muted)]">{queryMeta.query}</span>}
              </div>
              {querySeries.map((s, i) => (
                <SeriesRow key={`${s.name}|${JSON.stringify(s.labels)}|${i}`} series={s} />
              ))}
            </div>
          )}
        </section>

        {/* 3. 活跃告警 */}
        <section className="vigil-card p-4">
          <div className="mb-3 flex items-center gap-2">
            <h3 className="text-sm font-medium">活跃告警</h3>
            {alertsLoaded && !alertsError && (
              <span className="rounded bg-[var(--vigil-muted-bg)] px-2 py-0.5 text-[11px] text-[var(--vigil-muted)]">
                {alerts.length} 条
              </span>
            )}
          </div>
          {!alertsLoaded ? null : alertsError &&
            alertsError.includes("未配置 Alertmanager") ? (
            <EmptyState
              icon={<AlertTriangle className="size-6" />}
              title="Alertmanager 未配置"
              description={alertsError}
              hint="在 config.yaml 配置 ops.prometheus.alertmanager 后显示实时告警快照。"
              className="py-8"
            />
          ) : alertsError ? (
            <EmptyState
              icon={<AlertTriangle className="size-6" />}
              title="告警加载失败"
              description={alertsError}
              className="py-8"
            />
          ) : alerts.length === 0 ? (
            <EmptyState
              icon={<AlertTriangle className="size-6" />}
              title="暂无活跃告警"
              description="Alertmanager 当前无活跃告警（实时快照）。"
              hint="watch 采集的告警流见 Incidents 页——两者语义不同：Incidents = 采集归档，本页 = 实时状态。"
              className="py-8"
            />
          ) : (
            <div className="space-y-2">
              {alerts.map((a, idx) => (
                <AlertRow
                  key={`${a.alertname}|${a.instance ?? ""}|${a.startsAt ?? idx}`}
                  alert={a}
                />
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
