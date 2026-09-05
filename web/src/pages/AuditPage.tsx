import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import "@/i18n";
import { translateBackendMessage } from "@/lib/backendMsg";
import { ChevronRight, History, Trash2, XCircle } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import type { AuditEvent, AuditSessionSummary } from "@/lib/api";
import { isMockEnabled, MOCK_AUDIT_EVENTS, MOCK_AUDIT_SESSIONS } from "@/lib/mock";
import { cn } from "@/lib/ops";

/**
 * Audit page (batch 28 contract, data source already exists — JSON-ified
 * trajectories): GET /api/audit/sessions → GET /api/audit/events?session_id=&type=
 * → GET /api/audit/events/{session_id}/{seq} (single event full payload, redacted).
 * List output only gets a preview (first 500 chars).
 */
export default function AuditPage() {
  const { t, i18n } = useTranslation();
  const blang = i18n.language === "en" ? "en" : "zh";
  const mock = isMockEnabled();
  const [sessions, setSessions] = useState<AuditSessionSummary[]>([]);
  const [selectedSession, setSelectedSession] = useState<string | null>(null);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [typeFilter, setTypeFilter] = useState("");
  const [detail, setDetail] = useState<AuditEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mockNote] = useState(mock);

  const loadSessions = useCallback(() => {
    setError(null);
    if (mock) {
      setSessions(MOCK_AUDIT_SESSIONS);
      setSelectedSession((prev) => prev ?? MOCK_AUDIT_SESSIONS[0].session_id);
      return;
    }
    api
      .getAuditSessions({ limit: 50 })
      .then((resp) => {
        if (resp.error) {
          setError(resp.error.message ?? t("audit.loadFailed"));
          return;
        }
        setSessions(resp.sessions ?? []);
        setSelectedSession((prev) => prev ?? (resp.sessions?.[0]?.session_id ?? null));
      })
      .catch((e: unknown) => {
        setError(
          e instanceof ApiError
            ? `[${e.code}] ${e.message}`
            : e instanceof Error
              ? e.message
              : String(e),
        );
      });
  }, [mock]);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  useEffect(() => {
    if (!selectedSession) {
      setEvents([]);
      return;
    }
    setError(null);
    if (mock) {
      setEvents(MOCK_AUDIT_EVENTS);
      return;
    }
    let alive = true;
    api
      .getAuditEvents({ session_id: selectedSession, type: typeFilter || undefined, limit: 100 })
      .then((resp) => {
        if (!alive) return;
        if (resp.error) {
          setError(resp.error.message ?? t("audit.loadFailed"));
          return;
        }
        setEvents(resp.events ?? []);
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setError(e instanceof ApiError ? `[${e.code}] ${e.message}` : e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, [selectedSession, typeFilter, mock]);

  // Event row click = expand/collapse toggle (clicking the same row again collapses)
  const openDetail = async (seq: number) => {
    if (!selectedSession) return;
    if (detail?.seq === seq) {
      setDetail(null);
      return;
    }
    setDetail(null);
    if (mock) {
      setDetail(MOCK_AUDIT_EVENTS.find((e) => e.seq === seq) ?? null);
      return;
    }
    try {
      setDetail(await api.getAuditEvent(selectedSession, seq));
    } catch (e) {
      setError(e instanceof ApiError ? `[${e.code}] ${e.message}` : e instanceof Error ? e.message : String(e));
    }
  };

  const prune = async () => {
    if (!selectedSession) return;
    setError(null);
    if (mock) return;
    try {
      await api.deleteAuditEvents({ session_id: selectedSession });
      await loadSessions();
      setEvents([]);
    } catch (e) {
      setError(e instanceof ApiError ? `[${e.code}] ${e.message}` : e instanceof Error ? e.message : String(e));
    }
  };

  const eventTypeClass = (t?: string) =>
    t === "terminal"
      ? "text-sky-600 dark:text-sky-400"
      : t === "approval"
        ? "text-amber-600 dark:text-amber-400"
        : t === "tool" || t === "tool_call"
          ? "text-emerald-600 dark:text-emerald-400"
          : t === "tool_result"
            ? "text-violet-600 dark:text-violet-400"
            : "text-[var(--vigil-muted)]";

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <History className="size-5 text-[var(--vigil-muted)]" />
          <h1 className="text-lg font-semibold">Audit</h1>
          <span className="text-xs text-[var(--vigil-muted)]">{t("audit.subtitle")}</span>
        </div>
        <span className="ml-auto text-xs text-[var(--vigil-muted)]">{t("audit.readonlyNote")}</span>
      </div>

      {mockNote && (
        <div className="mb-3 inline-flex w-fit items-center gap-1.5 rounded border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[11px] text-amber-600 dark:text-amber-400">
          {t("audit.mockBadge")}
        </div>
      )}

      {error && (
        <div className="mb-3 flex items-center gap-2 rounded-md border border-red-500/50 bg-red-500/10 px-3 py-2 text-xs">
          <XCircle className="size-4 shrink-0 text-red-500" />
          <span>{translateBackendMessage(error, blang)}</span>
        </div>
      )}

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[280px_1fr]">
        {/* Session list */}
        <div className="scroll-thin min-h-0 overflow-y-auto">
          <div className="mb-2 flex items-center justify-between px-1 text-xs text-[var(--vigil-muted)]">
            <span>{t("audit.sessionsHeading", { n: sessions.length })}</span>
            <button
              type="button"
              onClick={prune}
              disabled={!selectedSession}
              className="vigil-btn h-6 px-2 text-xs disabled:opacity-40"
              title={t("audit.pruneTitle")}
            >
              <Trash2 className="size-3" /> prune
            </button>
          </div>
          {sessions.length === 0 ? (
            <div className="rounded-md border border-dashed border-[var(--vigil-border)] p-6 text-center text-xs text-[var(--vigil-muted)]">
              {t("audit.noSessions")}
            </div>
          ) : (
            <div className="vigil-card">
              {sessions.map((s) => (
                <button
                  key={s.session_id}
                  type="button"
                  onClick={() => {
                    setSelectedSession(s.session_id);
                    setDetail(null);
                  }}
                  className={cn(
                    "flex w-full flex-col gap-0.5 border-b border-[var(--vigil-border)] px-3 py-2 text-left last:border-b-0 hover:bg-[var(--vigil-muted-bg)]",
                    selectedSession === s.session_id && "bg-[var(--vigil-muted-bg)]",
                  )}
                >
                  <span className="font-mono text-[11px] text-[var(--vigil-text)]">{s.session_id}</span>
                  <span className="text-[11px] text-[var(--vigil-muted)]">
                    {t("audit.eventCount", { n: s.event_count })}
                    {s.started_at ? ` · ${String(s.started_at).slice(0, 16)}` : ""}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Event list + detail */}
        <div className="flex min-h-0 flex-col gap-3">
          <div className="flex items-center gap-2">
            <select
              value={typeFilter}
              onChange={(e) => {
                setTypeFilter(e.target.value);
                setDetail(null); // filter change: clear detail, list refetches
              }}
              className="vigil-input h-8 w-36 text-xs"
            >
              <option value="">{t("audit.allTypes")}</option>
              <option value="terminal">terminal</option>
              <option value="tool_call">tool_call</option>
              <option value="tool_result">tool_result</option>
              <option value="approval">approval</option>
              <option value="llm">llm</option>
            </select>
            <span className="text-xs text-[var(--vigil-muted)]">
              {t("audit.sessionLabel")} <span className="font-mono">{selectedSession ?? "-"}</span> · {t("audit.eventCount", { n: events.length })}
            </span>
          </div>

          <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
            {events.length === 0 ? (
              <div className="rounded-md border border-dashed border-[var(--vigil-border)] p-8 text-center text-xs text-[var(--vigil-muted)]">
                {t("audit.noEvents")}
              </div>
            ) : (
              <div className="vigil-card">
                {events.map((ev) => (
                  <button
                    key={ev.seq}
                    type="button"
                    onClick={() => openDetail(ev.seq)}
                    className="flex w-full items-center gap-2 border-b border-[var(--vigil-border)] px-3 py-1.5 text-left text-xs last:border-b-0 hover:bg-[var(--vigil-muted-bg)]"
                  >
                    <span className="font-mono text-[var(--vigil-muted)]">#{ev.seq}</span>
                    <span className={cn("w-16 shrink-0 font-medium", eventTypeClass(ev.type))}>
                      {ev.type ?? "-"}
                    </span>
                    <span className="min-w-0 flex-1 truncate font-mono text-[var(--vigil-text)] opacity-85">
                      {ev.command ?? JSON.stringify(ev).slice(0, 80)}
                    </span>
                    <ChevronRight className="size-3 shrink-0 text-[var(--vigil-muted)]" />
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Event detail (full payload, redacted) */}
          {detail && (
            <div className="vigil-card scroll-thin max-h-72 overflow-y-auto p-3">
              <div className="mb-2 flex items-center gap-2 text-xs font-medium">
                <History className="size-3.5 text-[var(--vigil-muted)]" />
                {t("audit.eventDetailHeading", { seq: detail.seq, type: detail.type ?? "-", ts: detail.ts ?? "" })}
              </div>
              {detail.command && (
                <pre className="mb-2 overflow-x-auto rounded bg-[var(--vigil-muted-bg)] p-2 font-mono text-[11px]">
                  {detail.command}
                </pre>
              )}
              <pre className="scroll-thin overflow-auto rounded bg-black/5 p-2 font-mono text-[11px] dark:bg-black/20">
                {JSON.stringify(detail, null, 2)}
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
