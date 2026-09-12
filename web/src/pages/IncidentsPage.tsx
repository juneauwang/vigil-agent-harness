import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import "@/i18n";
import { translateBackendMessage } from "@/lib/backendMsg";
import { CheckCircle2, TriangleAlert, Undo2, X } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import type { IncidentItem } from "@/lib/api";
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

      {actionError && (
        <div className="mb-3 rounded-md border border-[var(--vigil-error)]/50 bg-[var(--vigil-error)]/10 px-3 py-2 text-xs text-[var(--vigil-error)]">
          {translateBackendMessage(actionError, blang)}
        </div>
      )}

      {!loaded ? null : error ? (
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
