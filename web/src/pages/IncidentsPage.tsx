import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import "@/i18n";
import { translateBackendMessage } from "@/lib/backendMsg";
import { CheckCircle2, TriangleAlert } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import type { IncidentItem } from "@/lib/api";
import { isMockEnabled } from "@/lib/mock";
import { EmptyState } from "@/components/EmptyState";
import { cn } from "@/lib/ops";

/**
 * Incidents page (batch 50): /api/incidents reads the watch inbox and returns
 * the alert list. Severity tier styling: critical=red / warning=yellow /
 * info=blue; empty inbox → "no alerts". `processed` is read-only display
 * (marking as handled goes through the watch_digest agent channel, not here).
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
  const { t } = useTranslation();
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
              {incident.alertname || t("incidents.unknownAlert")}
            </span>
            {incident.processed && (
              <span className="inline-flex shrink-0 items-center gap-1 text-[10px] text-[var(--vigil-ok)]">
                <CheckCircle2 className="size-3" /> {t("incidents.processedBadge")}
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
