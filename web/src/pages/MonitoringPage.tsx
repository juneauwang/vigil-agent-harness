import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router";
import { useTranslation } from "react-i18next";
import "@/i18n";
import { Activity, AlertTriangle, Loader2, Play, RefreshCw, Search, Server, Settings, X } from "lucide-react";
import { ApiError, api } from "@/lib/api";
import type {
  AlertDisposition,
  MonitoringAlert,
  MonitoringHealthService,
  MonitoringSeries,
} from "@/lib/api";
import { EmptyState } from "@/components/EmptyState";
import { cn, isAlertmanagerUnconfiguredError, isPrometheusUnavailableError } from "@/lib/ops";
import { translateBackendMessage } from "@/lib/backendMsg";

/**
 * Monitoring page (OPS-DELTA #78): service health / PromQL query / active alerts.
 *
 * Three-block layout (Grafana mental model + the existing blue-gray theme):
 * 1. Service health list — topology services probed on demand (30s backend
 *    cache), up green / down red / unknown gray; top summary + status filter +
 *    30s auto refresh.
 * 2. PromQL query — promql + duration/step → structured series table + SVG
 *    sparkline (hand-rolled, no chart library); guidance shown when Prometheus
 *    is not configured.
 * 3. Active alerts — live Alertmanager snapshot, severity colors reuse the
 *    Incidents tier styles. No monitoring data / all-unknown doesn't crash
 *    (empty states + guidance copy).
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

/** Hand-rolled SVG sparkline (no chart library): series time-point polyline, null for single/empty values. */
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
  const { t } = useTranslation();
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
          {series.point_count} {t("monitoring.pointsUnit")} · last {fmtNum(series.summary.last)} · min{" "}
          {fmtNum(series.summary.min)} · max {fmtNum(series.summary.max)}
        </div>
      </div>
      <Sparkline points={series.points} />
    </div>
  );
}

function DispositionLine({ disp, onExecute }: {
  disp: AlertDisposition | null | undefined;
  onExecute: (d: AlertDisposition) => void;
}) {
  const { t } = useTranslation();
  if (!disp || !disp.matched || !disp.runbook) {
    return (
      <div className="mt-1.5 rounded bg-[var(--vigil-muted-bg)] px-2 py-1 text-[11px] text-[var(--vigil-muted)]">
        {t("monitoring.dispNoMatch")}
      </div>
    );
  }
  const confidence =
    disp.confidence === "high"
      ? { label: "high", cls: "bg-[var(--vigil-primary)] text-white" }
      : { label: "medium", cls: "border border-[var(--vigil-primary)] text-[var(--vigil-primary)]" };
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-1.5 rounded bg-[var(--vigil-muted-bg)] px-2 py-1">
      <span className="text-[10px] text-[var(--vigil-muted)]">{t("monitoring.dispLabel")}</span>
      <Link
        to={`/runbooks?name=${encodeURIComponent(disp.runbook)}`}
        className="inline-flex items-center rounded bg-[var(--vigil-primary)] px-1.5 py-0.5 text-[11px] font-medium text-white hover:opacity-90"
        title={t("monitoring.dispViewSop")}
      >
        {disp.runbook}
      </Link>
      <span
        className={cn(
          "inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
          confidence.cls,
        )}
      >
        {confidence.label}
      </span>
      <span className="rounded border border-[var(--vigil-border)] px-1 py-0.5 text-[10px] text-[var(--vigil-muted)]">
        {disp.matched_by === "trigger" ? t("monitoring.dispTrigger") : t("monitoring.dispFuzzy")}
      </span>
      {disp.title && (
        <span className="min-w-0 flex-1 truncate text-[11px] text-[var(--vigil-muted)]">
          {disp.title}
        </span>
      )}
      <button
        type="button"
        className="inline-flex items-center gap-1 rounded border border-[var(--vigil-border)] px-1.5 py-0.5 text-[11px] text-[var(--vigil-text)] hover:border-[var(--vigil-primary)] hover:text-[var(--vigil-primary)]"
        onClick={() => onExecute(disp)}
        title={t("monitoring.dispExecuteTitle")}
      >
        <Play className="size-3" /> {t("monitoring.dispExecute")}
      </button>
    </div>
  );
}

function AlertRow({ alert, onExecute }: {
  alert: MonitoringAlert;
  onExecute: (d: AlertDisposition) => void;
}) {
  const { t } = useTranslation();
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
          {alert.alertname || t("monitoring.unknownAlert")}
        </div>
        <div className="mt-0.5 truncate text-[11px] text-[var(--vigil-muted)]">
          {[alert.instance, alert.startsAt && new Date(alert.startsAt).toLocaleString()]
            .filter(Boolean)
            .join(" · ") || "—"}
        </div>
        <DispositionLine disp={alert.disposition} onExecute={onExecute} />
      </div>
    </div>
  );
}

type StatusFilter = "all" | "up" | "down" | "unknown";

export default function MonitoringPage() {
  const { t, i18n } = useTranslation();
  // Option C: backend messages arrive in zh (backend default); translate for en UI.
  const blang = i18n.language === "en" ? "en" : "zh";
  const bmsg = (m: string | null) => (m ? translateBackendMessage(m, blang) : undefined);
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
  const [alertsMatched, setAlertsMatched] = useState(0);
  // batch87: executing a suggestion = after manual confirmation, goes through the existing runbook execution API (matrix / approval gates).
  const [execTarget, setExecTarget] = useState<AlertDisposition | null>(null);
  const [execRunning, setExecRunning] = useState(false);
  const [execInfo, setExecInfo] = useState<string | null>(null);
  // UI 监控集成设置（OPS-DELTA #107 配套：免手改 config.yaml）。
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [endpointDraft, setEndpointDraft] = useState("");
  const [alertmanagerDraft, setAlertmanagerDraft] = useState("");
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [settingsMsg, setSettingsMsg] = useState<string | null>(null);
  const [settingsErr, setSettingsErr] = useState<string | null>(null);

  const openSettings = useCallback(async () => {
    setSettingsOpen((v) => {
      if (v) return v; // already open — keep drafts as-is
      setSettingsErr(null);
      setSettingsMsg(null);
      void api
        .getMonitoringConfig()
        .then((resp) => {
          setEndpointDraft(resp.data?.endpoint ?? "");
          setAlertmanagerDraft(resp.data?.alertmanager ?? "");
        })
        .catch(() => setEndpointDraft(""));
      return true;
    });
  }, []);

  const saveSettings = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      const endpoint = endpointDraft.trim();
      const alertmanager = alertmanagerDraft.trim();
      setSettingsBusy(true);
      setSettingsErr(null);
      setSettingsMsg(null);
      try {
        const resp = await api.saveMonitoringConfig({ endpoint, alertmanager });
        if (resp.error) {
          setSettingsErr(bmsg(resp.error.message ?? resp.error.code ?? ""));
        } else {
          setSettingsMsg(t("monitoring.settingsSaved"));
          setEndpointDraft(resp.data?.endpoint ?? endpoint);
          setAlertmanagerDraft(resp.data?.alertmanager ?? alertmanager);
        }
      } catch (err) {
        setSettingsErr(errText(err));
      } finally {
        setSettingsBusy(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [endpointDraft, alertmanagerDraft, t],
  );

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
      .getMonitoringAlertsTriage()
      .then((resp) => {
        setAlerts(resp.data?.alerts ?? []);
        setAlertsMatched(resp.data?.matched_count ?? 0);
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
    isPrometheusUnavailableError(queryError);

  const confirmExecute = () => {
    if (!execTarget?.runbook || execRunning) return;
    setExecRunning(true);
    setExecInfo(null);
    const alert = alerts.find((a) => a.disposition === execTarget);
    api
      .runRunbook(execTarget.runbook, undefined, alert
        ? {
            alertname: alert.alertname,
            severity: alert.severity,
            instance: alert.instance ?? "",
            summary: alert.summary ?? "",
          }
        : undefined)
      .then((resp) => {
        setExecTarget(null);
        setExecRunning(false);
        setExecInfo(
          resp.ok
            ? t("monitoring.execStarted", { name: execTarget.runbook })
            : resp.error ?? t("monitoring.execStartFailed"),
        );
      })
      .catch((e: unknown) => {
        setExecTarget(null);
        setExecRunning(false);
        setExecInfo(errText(e));
      });
  };

  return (
    <div className="mx-auto w-full max-w-5xl">
      <div className="mb-4 flex items-center gap-2">
        <Activity className="size-5 text-[var(--vigil-muted)]" />
        <h1 className="text-lg font-semibold">Monitoring</h1>
        <span className="text-xs text-[var(--vigil-muted)]">{t("monitoring.subtitle")}</span>
      </div>

      <div className="space-y-4">
        {/* 1. Service health */}
        <section className="vigil-card p-4">
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <h3 className="text-sm font-medium">{t("monitoring.healthTitle")}</h3>
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
                {t("monitoring.allFilter", { n: health.length })}
              </button>
            </div>
            <button
              type="button"
              onClick={() => loadHealth(true)}
              className="ml-auto inline-flex items-center gap-1 rounded border border-[var(--vigil-border)] px-2 py-1 text-[11px] text-[var(--vigil-muted)] hover:text-[var(--vigil-text)]"
            >
              <RefreshCw className="size-3" /> {t("common.refresh")}
            </button>
            <button
              type="button"
              onClick={() => void openSettings()}
              className="inline-flex items-center gap-1 rounded border border-[var(--vigil-border)] px-2 py-1 text-[11px] text-[var(--vigil-muted)] hover:text-[var(--vigil-text)]"
            >
              <Settings className="size-3" /> {t("monitoring.settingsBtn")}
            </button>
          </div>

          {settingsOpen && (
            <div className="mb-2 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)] p-3 text-xs shadow-[var(--vigil-shadow)]">
              <div className="mb-2 font-medium">{t("monitoring.settingsTitle")}</div>
              <form onSubmit={(e) => void saveSettings(e)} className="space-y-2">
                <label className="block">
                  <span className="mb-1 block text-[var(--vigil-muted)]">
                    {t("monitoring.settingsEndpoint")}
                  </span>
                  <input
                    value={endpointDraft}
                    onChange={(e) => setEndpointDraft(e.target.value)}
                    placeholder={t("monitoring.settingsEndpointPh")}
                    spellCheck={false}
                    className="h-8 w-full rounded border border-[var(--vigil-border)] bg-[var(--vigil-bg)] px-2 text-xs text-[var(--vigil-text)] outline-none placeholder:text-[var(--vigil-muted)]/60"
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-[var(--vigil-muted)]">
                    {t("monitoring.settingsAlertmanager")}
                  </span>
                  <input
                    value={alertmanagerDraft}
                    onChange={(e) => setAlertmanagerDraft(e.target.value)}
                    placeholder={t("monitoring.settingsAlertmanagerPh")}
                    spellCheck={false}
                    className="h-8 w-full rounded border border-[var(--vigil-border)] bg-[var(--vigil-bg)] px-2 text-xs text-[var(--vigil-text)] outline-none placeholder:text-[var(--vigil-muted)]/60"
                  />
                </label>
                {settingsErr ? (
                  <div className="text-red-500">{settingsErr}</div>
                ) : settingsMsg ? (
                  <div className="text-emerald-500">{settingsMsg}</div>
                ) : null}
                <div className="flex items-center gap-2">
                  <button
                    type="submit"
                    disabled={settingsBusy}
                    className="vigil-btn inline-flex h-7 items-center gap-1 rounded border border-[var(--vigil-primary)]/40 px-2 text-[11px] text-[var(--vigil-primary)] disabled:opacity-50"
                  >
                    {settingsBusy ? (
                      <Loader2 className="size-3 animate-spin" />
                    ) : null}
                    {t("monitoring.settingsSave")}
                  </button>
                  <button
                    type="button"
                    onClick={() => setSettingsOpen(false)}
                    className="inline-flex h-7 items-center rounded px-2 text-[11px] text-[var(--vigil-muted)] hover:text-[var(--vigil-text)]"
                  >
                    {t("monitoring.settingsCancel")}
                  </button>
                </div>
              </form>
            </div>
          )}

          {!healthLoaded ? (
            <div className="flex items-center gap-2 py-8 text-sm text-[var(--vigil-muted)]">
              <Loader2 className="size-4 animate-spin" />
              {t("monitoring.healthLoading")}
              <span className="text-xs text-[var(--vigil-muted)]/70">
                {t("monitoring.healthLoadingDesc")}
              </span>
            </div>
          ) : healthError ? (
            <EmptyState
              icon={<Activity className="size-6" />}
              title={t("monitoring.healthLoadFailedTitle")}
              description={bmsg(healthError)}
              hint={t("monitoring.healthLoadFailedHint")}
              className="py-10"
            />
          ) : health.length === 0 ? (
            <EmptyState
              icon={<Server className="size-6" />}
              title={t("monitoring.noServicesTitle")}
              description={t("monitoring.noServicesDesc")}
              hint={t("monitoring.noServicesHint")}
              className="py-10"
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-[var(--vigil-border)] text-[10px] uppercase tracking-wide text-[var(--vigil-muted)]">
                    <th className="py-1.5 pr-2 font-medium">{t("monitoring.thService")}</th>
                    <th className="px-2 py-1.5 font-medium">{t("monitoring.thType")}</th>
                    <th className="px-2 py-1.5 font-medium">{t("monitoring.thCluster")}</th>
                    <th className="px-2 py-1.5 font-medium">endpoint</th>
                    <th className="px-2 py-1.5 font-medium">{t("monitoring.thStatus")}</th>
                    <th className="px-2 py-1.5 font-medium">{t("monitoring.thLatency")}</th>
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

        {/* 2. PromQL query */}
        <section className="vigil-card p-4">
          <h3 className="mb-3 text-sm font-medium">{t("monitoring.promqlTitle")}</h3>
          <form onSubmit={runQuery} className="mb-3 flex flex-wrap items-center gap-2">
            <div className="relative min-w-0 flex-1">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-[var(--vigil-muted)]" />
              <input
                value={promql}
                onChange={(e) => setPromql(e.target.value)}
                placeholder={t("monitoring.promqlPlaceholder")}
                className="vigil-input w-full rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)] py-1.5 pl-8 pr-3 text-xs outline-none focus:border-[var(--vigil-primary)]"
              />
            </div>
            <input
              value={duration}
              onChange={(e) => setDuration(e.target.value)}
              placeholder="duration 30m"
              title={t("monitoring.durationTitle")}
              className="vigil-input w-28 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)] px-2 py-1.5 text-xs outline-none focus:border-[var(--vigil-primary)]"
            />
            <input
              value={step}
              onChange={(e) => setStep(e.target.value)}
              placeholder="step 60s"
              title={t("monitoring.stepTitle")}
              className="vigil-input w-28 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)] px-2 py-1.5 text-xs outline-none focus:border-[var(--vigil-primary)]"
            />
            <button
              type="submit"
              disabled={querying || !promql.trim()}
              className="inline-flex items-center gap-1 rounded-md bg-[var(--vigil-primary)] px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
            >
              <Play className="size-3" /> {querying ? t("monitoring.querying") : t("monitoring.exec")}
            </button>
          </form>

          {isPromUnavailable ? (
            <EmptyState
              icon={<Search className="size-6" />}
              title={t("monitoring.promUnavailableTitle")}
              description={bmsg(queryError)}
              hint={t("monitoring.promUnavailableHint")}
              className="py-8"
            />
          ) : queryError ? (
            <EmptyState
              icon={<AlertTriangle className="size-6" />}
              title={t("monitoring.queryFailedTitle")}
              description={bmsg(queryError)}
              hint={t("monitoring.queryFailedHint")}
              className="py-8"
            />
          ) : !queryDone ? (
            <div className="py-6 text-center text-xs text-[var(--vigil-muted)]">
              {t("monitoring.queryIntro")}
            </div>
          ) : querySeries.length === 0 ? (
            <EmptyState
              icon={<Search className="size-6" />}
              title={t("monitoring.noSeriesTitle")}
              description={t("monitoring.noSeriesDesc", { query: queryMeta?.query ?? "", duration: queryMeta?.duration ?? "", step: queryMeta?.step ?? "" })}
              className="py-8"
            />
          ) : (
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <span className="text-[11px] text-[var(--vigil-muted)]">
                  {t("monitoring.seriesCount", { n: querySeries.length, range: `${queryMeta?.duration ?? ""}/${queryMeta?.step ?? ""}` })}
                </span>
                {queryMeta && <span className="truncate font-mono text-[11px] text-[var(--vigil-muted)]">{queryMeta.query}</span>}
              </div>
              {querySeries.map((s, i) => (
                <SeriesRow key={`${s.name}|${JSON.stringify(s.labels)}|${i}`} series={s} />
              ))}
            </div>
          )}
        </section>

        {/* 3. Active alerts */}
        <section className="vigil-card p-4">
          <div className="mb-3 flex items-center gap-2">
            <h3 className="text-sm font-medium">{t("monitoring.alertsTitle")}</h3>
            {alertsLoaded && !alertsError && (
              <span className="rounded bg-[var(--vigil-muted-bg)] px-2 py-0.5 text-[11px] text-[var(--vigil-muted)]">
                {t("monitoring.alertsCount", { n: alerts.length })}
                {alertsMatched > 0 ? t("monitoring.alertsMatched", { n: alertsMatched }) : ""}
              </span>
            )}
            {execInfo && (
              <span className="rounded bg-[var(--vigil-muted-bg)] px-2 py-0.5 text-[11px] text-[var(--vigil-text)]">
                {bmsg(execInfo)}
              </span>
            )}
          </div>
          {!alertsLoaded ? null : alertsError &&
            isAlertmanagerUnconfiguredError(alertsError) ? (
            <EmptyState
              icon={<AlertTriangle className="size-6" />}
              title={t("monitoring.promUnavailableTitle")}
              description={bmsg(alertsError)}
              hint={t("monitoring.alertmanagerHint")}
              className="py-8"
            />
          ) : alertsError ? (
            <EmptyState
              icon={<AlertTriangle className="size-6" />}
              title={t("monitoring.alertsLoadFailedTitle")}
              description={bmsg(alertsError)}
              className="py-8"
            />
          ) : alerts.length === 0 ? (
            <EmptyState
              icon={<AlertTriangle className="size-6" />}
              title={t("monitoring.noAlertsTitle")}
              description={t("monitoring.noAlertsDesc")}
              hint={t("monitoring.noAlertsHint")}
              className="py-8"
            />
          ) : (
            <div className="space-y-2">
              {alerts.map((a, idx) => (
                <AlertRow
                  key={`${a.alertname}|${a.instance ?? ""}|${a.startsAt ?? idx}`}
                  alert={a}
                  onExecute={(d) => setExecTarget(d)}
                />
              ))}
            </div>
          )}
        </section>
      </div>
      {execTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-md rounded-lg border border-[var(--vigil-border)] bg-[var(--vigil-card)] p-5 shadow-lg">
            <div className="flex items-center gap-2">
              <Play className="size-4 text-[var(--vigil-primary)]" />
              <h2 className="text-sm font-semibold">{t("monitoring.execModalTitle")}</h2>
              <button
                className="ml-auto rounded p-1 hover:bg-[var(--vigil-muted-bg)]"
                onClick={() => setExecTarget(null)}
                aria-label={t("common.close")}
                disabled={execRunning}
              >
                <X className="size-4" />
              </button>
            </div>
            <p className="mt-3 text-sm text-[var(--vigil-text)] opacity-80">
              {t("monitoring.execConfirmPrefix")}
              <code className="font-mono">{execTarget.runbook}</code>{t("monitoring.execConfirmSuffix")}
            </p>
            <p className="mt-2 text-xs text-[var(--vigil-muted)]">
              {t("monitoring.matchBasis", { reason: execTarget.reason ?? execTarget.hint ?? "—" })}
            </p>
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                className="vigil-btn border border-[var(--vigil-border)] px-3 py-1 text-xs"
                onClick={() => setExecTarget(null)}
                disabled={execRunning}
              >
                {t("common.cancel")}
              </button>
              <button
                type="button"
                className="vigil-btn px-3 py-1 text-xs"
                onClick={confirmExecute}
                disabled={execRunning}
              >
                {execRunning ? <Loader2 className="mr-1 inline size-3.5 animate-spin" /> : null}
                {t("monitoring.confirmExec")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
