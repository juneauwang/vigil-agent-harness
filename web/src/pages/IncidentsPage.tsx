import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import "@/i18n";
import { translateBackendMessage } from "@/lib/backendMsg";
import { CheckCircle2, ChevronDown, ChevronRight, ShieldAlert, TriangleAlert, Undo2, X } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import type { AutodispatchAuditEntry, AutodispatchAuditStep, IncidentItem } from "@/lib/api";
import { isMockEnabled } from "@/lib/mock";
import { EmptyState } from "@/components/EmptyState";
import { cn } from "@/lib/ops";

/**
 * Incidents page (batch 50): /api/incidents reads the watch inbox and returns
 * the alert list. Severity tier styling: critical=red / warning=yellow /
 * info=blue; empty inbox → "no alerts". `processed` stays read-only display
 * (marking goes through the watch_digest agent channel).
 *
 * task33: manual disposition — each row gets 确认(ack)/清除(clear). State lives
 * in sqlite (`incident_marks`, POST /api/incidents/mark), NOT in the inbox
 * snapshot: writing back to the snapshot would lose the ack / resurrect the
 * alert on the next collection (see tools/watch_marks.py). Clear is two-step
 * (arm → confirm within 3s), no modal.
 */

/** Row identity for local UI state (matches the list key). */
function rowKey(incident: IncidentItem, idx: number): string {
  return `${incident.alertname ?? "?"}|${incident.instance ?? "?"}|${incident.startsAt ?? idx}`;
}

const CLEAR_CONFIRM_MS = 3000;

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

interface IncidentRowProps {
  incident: IncidentItem;
  busy: boolean;
  clearArmed: boolean;
  onAck: () => void;
  onUnmark: () => void;
  onClear: () => void;
}

function IncidentRow({ incident, busy, clearArmed, onAck, onUnmark, onClear }: IncidentRowProps) {
  const { t } = useTranslation();
  const badge = severityBadge(incident.severity);
  const acked = incident.mark === "ack";
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
              {incident.alertname || t("incidents.unknownAlert")}
            </span>
            {incident.processed && (
              <span className="inline-flex shrink-0 items-center gap-1 text-[10px] text-[var(--vigil-ok)]">
                <CheckCircle2 className="size-3" /> {t("incidents.processedBadge")}
              </span>
            )}
            {acked && (
              <span className="inline-flex shrink-0 items-center gap-1 text-[10px] text-[var(--vigil-ok)]">
                <CheckCircle2 className="size-3" /> {t("incidents.markAckBadge")}
              </span>
            )}
          </div>
          <div className="mt-1.5 grid grid-cols-1 gap-x-6 gap-y-1 text-xs text-[var(--vigil-muted)] sm:grid-cols-2">
            <div className="truncate">
              <span className="text-[var(--vigil-muted)]">{t("incidents.fldInstance")}</span>
              <span className="text-[var(--vigil-text)]">{incident.instance || "-"}</span>
            </div>
            <div className="truncate">
              <span className="text-[var(--vigil-muted)]">{t("incidents.fldState")}</span>
              <span className="text-[var(--vigil-text)]">{incident.state || "-"}</span>
            </div>
            <div className="truncate">
              <span className="text-[var(--vigil-muted)]">{t("incidents.fldStartsAt")}</span>
              <span className="text-[var(--vigil-text)]">{incident.startsAt || "-"}</span>
            </div>
            <div className="truncate">
              <span className="text-[var(--vigil-muted)]">{t("incidents.fldCollectedAt")}</span>
              <span className="text-[var(--vigil-text)]">{incident.collected_at || "-"}</span>
            </div>
            <div className="truncate">
              <span className="text-[var(--vigil-muted)]">{t("incidents.fldSource")}</span>
              <span className="text-[var(--vigil-text)]">{incident.source || "-"}</span>
            </div>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {acked ? (
            <button
              type="button"
              disabled={busy}
              onClick={onUnmark}
              className="vigil-btn inline-flex h-7 items-center gap-1 border border-[var(--vigil-border)] px-2 text-xs text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] disabled:opacity-50"
            >
              <Undo2 className="size-3" /> {t("incidents.markUndo")}
            </button>
          ) : (
            <button
              type="button"
              disabled={busy}
              onClick={onAck}
              className="vigil-btn inline-flex h-7 items-center gap-1 border border-[var(--vigil-border)] px-2 text-xs text-[var(--vigil-text)] hover:bg-[var(--vigil-muted-bg)] disabled:opacity-50"
            >
              <CheckCircle2 className="size-3" /> {t("incidents.markAck")}
            </button>
          )}
          <button
            type="button"
            disabled={busy}
            onClick={onClear}
            className={cn(
              "vigil-btn inline-flex h-7 items-center gap-1 border px-2 text-xs disabled:opacity-50",
              clearArmed
                ? "border-[var(--vigil-error)] bg-[var(--vigil-error)] text-white"
                : "border-[var(--vigil-border)] text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]",
            )}
          >
            <X className="size-3" /> {clearArmed ? t("incidents.markConfirm") : t("incidents.markClear")}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── task35 PART C: 自动派发记录（告警驱动的 runbook 自动执行，无会话）──────────
//
// 自动派发是无人值守事件（no_agent cron），不是对话——所以不进会话列表，落点
// 在本页"事后回看"。每条含时间/告警/实例/命中 runbook/result/needs_human/耗时，
// 展开看每步（步骤标识 + 结果 + 截断后的输出）。needs_human=true 视觉上明显
// 不同：它意味着引擎拒绝执行（预审缺失/哈希漂移）或执行失败、已降级回建议
// 闭环——最需要人看到的一条。

/** result → 文案（复用 runbooks 的结果词表；未知原样显示）。 */
function autoDispatchResultLabel(
  result: string | undefined,
  t: (k: string) => string,
): string {
  switch ((result ?? "").toLowerCase()) {
    case "ok":
      return t("runbooks.result.ok");
    case "rolled_back":
      return t("runbooks.result.rolled_back");
    case "blocked":
      return t("runbooks.result.blocked");
    case "failed":
      return t("runbooks.result.failed");
    default:
      return result || t("runbooks.result.unknown");
  }
}

function autoDispatchResultClass(result: string | undefined): string {
  switch ((result ?? "").toLowerCase()) {
    case "ok":
      return "text-[var(--vigil-ok)]";
    case "rolled_back":
    case "blocked":
      return "text-[var(--vigil-warn)]";
    default:
      return "text-[var(--vigil-error)]";
  }
}

const TRUNCATED_STEP_ID = "__truncated__";

function AuditStepList({ steps }: { steps: AutodispatchAuditStep[] }) {
  const { t } = useTranslation();
  if (steps.length === 0) {
    return (
      <p className="mt-2 border-t border-[var(--vigil-border)] pt-2 text-xs text-[var(--vigil-muted)]">
        {t("incidents.noSteps")}
      </p>
    );
  }
  return (
    <ol className="mt-2 space-y-2 border-t border-[var(--vigil-border)] pt-2">
      {steps.map((st, i) => {
        if (st.id === TRUNCATED_STEP_ID) {
          return (
            <li key="__truncated__" className="text-xs italic text-[var(--vigil-muted)]">
              {t("incidents.autoDispatchTruncated", { n: st.omitted_steps ?? 0 })}
            </li>
          );
        }
        const ok = st.ok === true || st.status === "ok";
        const fail = st.status === "failed" || st.status === "blocked";
        return (
          <li key={`${st.id ?? "step"}-${i}`} className="text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <span
                className={cn(
                  "shrink-0 rounded px-1.5 py-px font-mono text-[10px]",
                  ok
                    ? "bg-[var(--vigil-muted-bg)] text-[var(--vigil-ok)]"
                    : fail
                      ? "bg-[var(--vigil-muted-bg)] text-[var(--vigil-error)]"
                      : "bg-[var(--vigil-muted-bg)] text-[var(--vigil-muted)]",
                )}
              >
                {st.status || "?"}
              </span>
              <span className="font-mono font-medium text-[var(--vigil-text)]">
                {st.id || "?"}
              </span>
              {st.action && (
                <span className="text-[var(--vigil-muted)]">· {st.action}</span>
              )}
              {st.target && (
                <span className="text-[var(--vigil-muted)]">
                  {t("incidents.stepTarget")}
                  {st.target}
                </span>
              )}
            </div>
            {st.error && (
              <div className="mt-0.5 break-words text-[var(--vigil-error)]">{st.error}</div>
            )}
            {(st.commands ?? []).map((c, ci) => (
              <div
                key={ci}
                className="mt-1 border-l border-[var(--vigil-border)] pl-2"
              >
                <div className="text-[var(--vigil-muted)]">
                  {c.desc || "-"}
                  {c.exit_code !== undefined && c.exit_code !== null && (
                    <span className="ml-2">
                      {t("incidents.stepExit")}
                      {String(c.exit_code)}
                    </span>
                  )}
                </div>
                {c.stdout ? (
                  <pre className="scroll-thin mt-0.5 max-h-40 overflow-auto whitespace-pre-wrap break-words text-[11px] text-[var(--vigil-muted)]">
                    <span className="text-[var(--vigil-muted)]">{t("incidents.stepStdout")}</span>
                    {c.stdout}
                  </pre>
                ) : null}
                {c.stderr ? (
                  <pre className="scroll-thin mt-0.5 max-h-40 overflow-auto whitespace-pre-wrap break-words text-[11px] text-[var(--vigil-error)]">
                    <span className="text-[var(--vigil-muted)]">{t("incidents.stepStderr")}</span>
                    {c.stderr}
                  </pre>
                ) : null}
              </div>
            ))}
          </li>
        );
      })}
    </ol>
  );
}

function AutoDispatchRow({ entry }: { entry: AutodispatchAuditEntry }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const steps = entry.steps ?? [];
  const needsHuman = entry.needs_human === true;
  return (
    <div
      className={cn(
        "rounded-md border p-3",
        needsHuman
          ? "border-[var(--vigil-warn)] bg-[var(--vigil-warn)]/10"
          : "border-[var(--vigil-border)] bg-[var(--vigil-card)]",
      )}
    >
      <div className="flex items-start gap-3">
        {needsHuman && (
          <span className="mt-0.5 inline-flex shrink-0 items-center gap-1 rounded bg-[var(--vigil-warn)] px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-[var(--vigil-dark)]">
            <ShieldAlert className="size-3" /> {t("incidents.needsHumanBadge")}
          </span>
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate text-sm font-medium text-[var(--vigil-text)]">
              {entry.alertname || t("incidents.unknownAlert")}
            </span>
            <span className="font-mono text-[10px] text-[var(--vigil-muted)]">
              {entry.ts || "-"}
            </span>
          </div>
          <div className="mt-1.5 grid grid-cols-1 gap-x-6 gap-y-1 text-xs text-[var(--vigil-muted)] sm:grid-cols-2">
            <div className="truncate">
              <span>{t("incidents.thInstance")}</span>
              <span className="ml-1 text-[var(--vigil-text)]">{entry.instance || "-"}</span>
            </div>
            <div className="truncate">
              <span>{t("incidents.thRunbook")}</span>
              <span className="ml-1 font-mono text-[var(--vigil-text)]">
                {entry.runbook || "-"}
              </span>
            </div>
            <div className="truncate">
              <span>{t("incidents.thResult")}</span>
              <span className={cn("ml-1 font-semibold", autoDispatchResultClass(entry.result))}>
                {autoDispatchResultLabel(entry.result, t)}
              </span>
            </div>
            <div className="truncate">
              <span>{t("incidents.thDuration")}</span>
              <span className="ml-1 text-[var(--vigil-text)]">
                {entry.duration_s !== undefined && entry.duration_s !== null
                  ? `${entry.duration_s}s`
                  : "-"}
              </span>
            </div>
            {entry.matched_keyword ? (
              <div className="truncate">
                <span>{t("incidents.matchedKeyword")}</span>
                <span className="ml-1 text-[var(--vigil-text)]">{entry.matched_keyword}</span>
              </div>
            ) : null}
          </div>
          {needsHuman && (
            <p className="mt-1.5 text-[11px] font-medium text-[var(--vigil-warn)]">
              {t("incidents.needsHumanNote")}
            </p>
          )}
          {entry.error && !needsHuman && (
            <p className="mt-1.5 break-words text-xs text-[var(--vigil-error)]">{entry.error}</p>
          )}
        </div>
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
          className="vigil-btn inline-flex h-7 shrink-0 items-center gap-1 border border-[var(--vigil-border)] px-2 text-xs text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]"
        >
          {open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
          {t("incidents.stepsToggle", { n: steps.length })}
        </button>
      </div>
      {open && <AuditStepList steps={steps} />}
    </div>
  );
}

export default function IncidentsPage() {
  const { t, i18n } = useTranslation();
  const blang = i18n.language === "en" ? "en" : "zh";
  const mock = isMockEnabled();
  const [items, setItems] = useState<IncidentItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const [clearArmed, setClearArmed] = useState<string | null>(null);
  const clearTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // task35：本页两个视图——告警（原有）/ 自动派发记录（新增）。
  const [tab, setTab] = useState<"alerts" | "autodispatch">("alerts");
  const [audit, setAudit] = useState<AutodispatchAuditEntry[]>([]);
  const [auditLoaded, setAuditLoaded] = useState(false);
  const [auditError, setAuditError] = useState<string | null>(null);

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
          setError(resp.error.message ?? t("incidents.loadFailed"));
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

  useEffect(() => {
    let alive = true;
    if (mock) {
      setAuditLoaded(true);
      return;
    }
    api
      .getAutodispatchAudit({ limit: 50 })
      .then((resp) => {
        if (!alive) return;
        setAudit(resp.data?.entries ?? []);
        setAuditLoaded(true);
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setAuditError(
          e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e),
        );
        setAuditLoaded(true);
      });
    return () => {
      alive = false;
    };
  }, [mock]);

  // 清除二次确认：3 秒不点第二下自动回落（不引入 modal 组件）。
  useEffect(() => {
    if (clearTimerRef.current) clearTimeout(clearTimerRef.current);
    if (!clearArmed) return;
    clearTimerRef.current = setTimeout(() => setClearArmed(null), CLEAR_CONFIRM_MS);
    return () => {
      if (clearTimerRef.current) clearTimeout(clearTimerRef.current);
    };
  }, [clearArmed]);

  const submitMark = useCallback(
    async (action: "ack" | "clear" | "unmark", incident: IncidentItem, key: string) => {
      setActionError(null);
      setPendingKey(key);
      try {
        const resp = await api.markIncident({
          action,
          alertname: incident.alertname ?? "",
          instance: incident.instance,
          startsAt: incident.startsAt,
        });
        if (resp.error) {
          setActionError(resp.error.message ?? t("incidents.markFailed"));
        } else {
          setItems(resp.incidents ?? []);
          setTotal(resp.total ?? 0);
        }
      } catch (e: unknown) {
        setActionError(
          e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e),
        );
      } finally {
        setPendingKey(null);
        setClearArmed(null);
      }
    },
    [t],
  );

  const handleClear = useCallback(
    (incident: IncidentItem, key: string) => {
      if (clearArmed !== key) {
        setClearArmed(key);
        return;
      }
      void submitMark("clear", incident, key);
    },
    [clearArmed, submitMark],
  );

  return (
    <div className="mx-auto w-full max-w-5xl">
      <div className="mb-4 flex items-center gap-2">
        <TriangleAlert className="size-5 text-[var(--vigil-muted)]" />
        <h1 className="text-lg font-semibold">Incidents</h1>
        <span className="text-xs text-[var(--vigil-muted)]">{t("incidents.subtitle")}</span>
        {loaded && !error && total > 0 && (
          <span className="ml-auto rounded bg-[var(--vigil-muted-bg)] px-2 py-0.5 text-xs text-[var(--vigil-muted)]">
            {t("incidents.totalBadge", { n: total })}
          </span>
        )}
      </div>

      <div role="tablist" className="mb-4 flex items-center gap-1.5">
        {(["alerts", "autodispatch"] as const).map((key) => {
          const active = tab === key;
          return (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => setTab(key)}
              className={cn(
                "vigil-btn inline-flex h-8 items-center gap-1.5 border px-3 text-xs",
                active
                  ? "border-[var(--vigil-primary)] text-[var(--vigil-text)]"
                  : "border-[var(--vigil-border)] text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]",
              )}
            >
              {key === "alerts" ? t("incidents.tabAlerts") : t("incidents.tabAutoDispatch")}
              {key === "autodispatch" && auditLoaded && audit.length > 0 && (
                <span className="rounded bg-[var(--vigil-muted-bg)] px-1.5 py-px text-[10px]">
                  {audit.length}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {actionError && tab === "alerts" && (
        <div className="mb-3 rounded-md border border-[var(--vigil-error)]/50 bg-[var(--vigil-error)]/10 px-3 py-2 text-xs text-[var(--vigil-error)]">
          {translateBackendMessage(actionError, blang)}
        </div>
      )}

      {tab === "autodispatch" ? (
        !auditLoaded ? null : auditError ? (
          <EmptyState
            icon={<TriangleAlert className="size-6" />}
            title={t("incidents.loadFailedTitle")}
            description={translateBackendMessage(auditError, blang)}
            hint={t("incidents.loadFailedHint")}
            className="py-16"
          />
        ) : audit.length === 0 ? (
          <EmptyState
            icon={<ShieldAlert className="size-6" />}
            title={t("incidents.autoDispatchEmptyTitle")}
            description={t("incidents.autoDispatchEmptyDesc")}
            hint={t("incidents.autoDispatchEmptyHint")}
            className="py-16"
          />
        ) : (
          <div className="space-y-2">
            <p className="text-xs text-[var(--vigil-muted)]">
              {t("incidents.autoDispatchNote")}
            </p>
            {audit.map((entry, idx) => (
              <AutoDispatchRow key={`${entry.ts ?? ""}-${entry.runbook ?? ""}-${idx}`} entry={entry} />
            ))}
          </div>
        )
      ) : !loaded ? null : error ? (
        <EmptyState
          icon={<TriangleAlert className="size-6" />}
          title={t("incidents.loadFailedTitle")}
          description={translateBackendMessage(error, blang)}
          hint={t("incidents.loadFailedHint")}
          className="py-16"
        />
      ) : items.length === 0 ? (
        <EmptyState
          icon={<TriangleAlert className="size-6" />}
          title={t("incidents.noAlertsTitle")}
          description={t("incidents.noAlertsDesc")}
          hint={t("incidents.noAlertsHint")}
          className="py-16"
        />
      ) : (
        <div className="space-y-2">
          {items.map((inc, idx) => {
            const key = rowKey(inc, idx);
            return (
              <IncidentRow
                key={key}
                incident={inc}
                busy={pendingKey === key}
                clearArmed={clearArmed === key}
                onAck={() => void submitMark("ack", inc, key)}
                onUnmark={() => void submitMark("unmark", inc, key)}
                onClear={() => handleClear(inc, key)}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}
