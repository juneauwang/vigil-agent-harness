import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import "@/i18n";
import { Check, ChevronDown, ChevronUp, EyeOff, ShieldAlert, X } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import type { ApprovalItem } from "@/lib/api";
import { EnvBadge } from "@/components/StatusBits";
import { cn } from "@/lib/ops";
import {
  dequeueApproval,
  dropResolvedApprovals,
  enqueueApprovals,
  initialApprovalQueue,
  setQueueBusy,
  setQueueError,
  type ApprovalQueueState,
} from "@/lib/approvalQueue";
import { approvalPoller, useApprovalSnapshot } from "@/lib/approvalPoller";
import { notifyApprovalResolved } from "@/lib/approvalEvents";
import {
  requestApprovalNotificationPermission,
  showApprovalNotification,
} from "@/lib/approvalNotification";

/**
 * Global approval modal (batch 34): page-centered modal visible on any route.
 *
 * Queue semantics: approvals reported by the poller enqueue one at a time;
 * after approve/deny succeeds it is removed and the next one pops; ignore =
 * removed from the queue (poller already marked seen, won't pop again this
 * session; the bell badge and approval center keep it, the command keeps
 * waiting, timeout policy per the existing timeout_policy). When the current
 * approval is resolved elsewhere / times out → auto-removed from the queue.
 * No ESC/overlay-click close — ignoring is an explicit action, avoiding
 * accidental dismissal that would hide the safety interaction.
 *
 * Batch 49: shape changed to a bottom-right card (Grafana notification style,
 * stackable queue); on approval arrival → desktop notification (Web
 * Notification API, permission requested on first use, falls back to the
 * in-page popup when denied); the popup carries a timeout countdown (derived
 * from timeout_at, red when close to expiry). Safety semantics unchanged: no
 * auto-dismiss, explicit approve/deny/ignore buttons, queue pops one at a
 * time, globally visible.
 */
function formatApiError(e: unknown): string {
  if (e instanceof ApiError) return `[${e.code}] ${e.message}`;
  return e instanceof Error ? e.message : String(e);
}

function queueReducer(
  state: ApprovalQueueState,
  action: { type: "enqueue"; added: ApprovalItem[] } | { type: "dequeue"; id: string } | { type: "drop"; pending: Set<string> } | { type: "busy"; busy: boolean } | { type: "error"; error: string | null },
): ApprovalQueueState {
  switch (action.type) {
    case "enqueue":
      return enqueueApprovals(state, action.added);
    case "dequeue":
      return dequeueApproval(state, action.id);
    case "drop":
      return dropResolvedApprovals(state, action.pending);
    case "busy":
      return setQueueBusy(state, action.busy);
    case "error":
      return setQueueError(state, action.error);
  }
}

/** Batch 41 §6: long commands render as a uniform monospace block + max-height scroll + expand full text (indentation preserved). */
function LongCommandBlock({ command }: { command: string }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const lines = (command ?? "").split("\n");
  const long = lines.length > 1 || (command ?? "").length > 80;
  if (!command) return null;
  const preview = expanded || !long ? command : lines[0].slice(0, 80) + "…";
  return (
    <div className="rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)] px-3 py-2">
      <pre
        className={cn(
          "scroll-thin overflow-y-auto whitespace-pre break-words font-mono text-xs leading-relaxed text-[var(--vigil-text)]",
          expanded ? "max-h-72" : "max-h-32",
        )}
      >
        {preview}
      </pre>
      {long && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mt-1 flex items-center gap-1 text-[10px] text-[var(--vigil-muted)] hover:text-[var(--vigil-text)]"
        >
          {expanded ? <ChevronUp className="size-3" /> : <ChevronDown className="size-3" />}
          {expanded ? t("common.collapse") : t("common.expandFull")}
        </button>
      )}
    </div>
  );
}

/** Batch 49: approval timeout countdown (derived from timeout_at; expired → "approval timed out, terminated"). */
function ApprovalCountdown({ timeoutAt, pending }: { timeoutAt?: string | null; pending: boolean }) {
  const { t } = useTranslation();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  if (!pending || !timeoutAt) return null;
  const deadline = Date.parse(timeoutAt);
  if (Number.isNaN(deadline)) return null;
  const ms = deadline - now;
  if (ms <= 0) return <span className="shrink-0 text-[10px] text-red-500">{t("approvals.timedOutTerminated")}</span>;
  const total = Math.ceil(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return (
    <span
      className={cn(
        "shrink-0 font-mono text-[10px]",
        total <= 60 ? "text-red-500" : "text-[var(--vigil-muted)]",
      )}
    >
      {m > 0 ? `${m}m ${s}s` : `${s}s`}
    </span>
  );
}

export default function ApprovalModal() {
  const { t } = useTranslation();
  const snap = useApprovalSnapshot();
  const [state, dispatch] = useReducer(queueReducer, initialApprovalQueue);
  const active = state.queue[0] ?? null;
  // Batch 41 §2: synchronous re-entry guard while submitting (state.busy only
  // disables the buttons on the next rendered frame; double-clicks/replays are
  // blocked by the ref).
  const inFlightRef = useRef(false);

  // New approvals reported by the poller → enqueue.
  useEffect(() => {
    if (snap.added.length > 0) {
      dispatch({ type: "enqueue", added: snap.added });
      // Batch 49: approval arrival → desktop notification (permission requested on first use; falls back to the in-page popup when denied).
      void (async () => {
        const granted = await requestApprovalNotificationPermission();
        if (granted) {
          for (const item of snap.added) showApprovalNotification(item);
        }
      })();
    }
  }, [snap.added]);

  // Current approval no longer pending (approved/denied/timed out elsewhere) → auto-removed from the queue.
  useEffect(() => {
    if (state.queue.length === 0) return;
    const pending = new Set(snap.approvals.map((a) => a.id));
    dispatch({ type: "drop", pending });
  }, [snap.approvals, state.queue.length]);

  const resolve = useCallback(
    async (item: ApprovalItem, action: "approved" | "denied") => {
      if (inFlightRef.current) return;
      inFlightRef.current = true;
      dispatch({ type: "busy", busy: true });
      dispatch({ type: "error", error: null });
      try {
        if (action === "approved") await api.approveApproval(item.id, "once");
        else await api.denyApproval(item.id);
        dispatch({ type: "dequeue", id: item.id });
        // Batch 42 §BH: broadcast the resolution — the chat page's approval
        // cards write back synchronously by approvalId; the popup closes and
        // the change applies globally without waiting for polling/history refetch.
        notifyApprovalResolved(item.id, action);
        // Refresh once manually so the badge/approval center don't wait for the next poll tick.
        void approvalPoller.refresh();
      } catch (e) {
        dispatch({ type: "error", error: formatApiError(e) });
      } finally {
        inFlightRef.current = false;
        dispatch({ type: "busy", busy: false });
      }
    },
    [],
  );

  const ignore = useCallback(() => {
    if (active && !state.busy) dispatch({ type: "dequeue", id: active.id });
  }, [active, state.busy]);

  if (!active) return null;

  return (
    <div
      data-testid="approval-corner"
      className="fixed bottom-4 right-4 z-[100] flex w-[400px] max-w-[calc(100vw-2rem)] flex-col gap-2"
      role="region"
      aria-label={t("approvals.regionAria")}
    >
      <div className="flex flex-col gap-3 rounded-lg border border-[var(--vigil-border)] bg-[var(--vigil-card)] p-4 shadow-[0_8px_30px_rgba(0,0,0,0.2)]">
        <div className="flex items-center gap-2">
          <ShieldAlert className="size-5 shrink-0 text-amber-500" />
          <span className="text-sm font-semibold text-[var(--vigil-text)]">{t("approvals.needsApproval")}</span>
          {active.grade && (
            <span className="ml-auto rounded bg-amber-500/15 px-1.5 py-px text-[10px] font-medium text-amber-600 dark:text-amber-400">
              L{active.grade}
            </span>
          )}
          <ApprovalCountdown
            key={active.id}
            timeoutAt={active.timeout_at}
            pending={active.status === "pending"}
          />
        </div>

        <LongCommandBlock command={active.command} />

        <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--vigil-muted)]">
          <EnvBadge env={active.env} />
          {active.description && (
            <span className="min-w-0 flex-1 whitespace-pre-wrap break-words">{active.description}</span>
          )}
        </div>

        {state.error && (
          <div className="rounded-md border border-red-500/50 bg-red-500/10 px-3 py-1.5 text-xs text-red-600 dark:text-red-400">
            {state.error}
          </div>
        )}

        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            disabled={state.busy}
            onClick={ignore}
            className="vigil-btn h-8 border border-[var(--vigil-border)] px-2.5 text-xs text-[var(--vigil-muted)] disabled:opacity-50"
          >
            <EyeOff className="size-3.5" /> {t("approvals.ignore")}
          </button>
          <button
            type="button"
            disabled={state.busy}
            onClick={() => void resolve(active, "denied")}
            className="vigil-btn h-8 whitespace-nowrap border border-red-500/50 px-2.5 text-xs text-red-600 dark:text-red-400 disabled:opacity-50"
          >
            <X className="size-3.5" /> {t("approvals.deny")}
          </button>
          <button
            type="button"
            disabled={state.busy}
            onClick={() => void resolve(active, "approved")}
            className="vigil-btn h-8 whitespace-nowrap border border-emerald-500/50 px-2.5 text-xs text-emerald-600 dark:text-emerald-400 disabled:opacity-50"
          >
            <Check className="size-3.5" /> {t("approvals.approve")}
          </button>
        </div>
      </div>
      {state.queue.length > 1 && (
        <div className="rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-3 py-1.5 text-[10px] text-[var(--vigil-muted)]">
          {t("approvals.queueMore", { n: state.queue.length - 1 })}
        </div>
      )}
    </div>
  );
}
