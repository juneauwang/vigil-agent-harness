import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type FormEvent, type SetStateAction } from "react";
import { useTranslation } from "react-i18next";
import "@/i18n";
import i18n from "@/i18n";
import { translateBackendMessage } from "@/lib/backendMsg";
import {
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  Loader2,
  MessageSquarePlus,
  Send,
  ShieldAlert,
  Square,
  TerminalSquare,
  User,
  Wrench,
  XCircle,
  X,
  Brain,
  HelpCircle,
  ListOrdered,
  Coins,
} from "lucide-react";
import { useSearchParams } from "react-router";
import { api, ApiError } from "@/lib/api";
import type { ChatContextUsage, ChatModelOption, ChatSessionSummary, ChatUsageResponse, UsageAnalyticsResponse } from "@/lib/api";
import {
  approvalIsTimedOut,
  applyChatEvent,
  chatInputDisabled,
  clarifyIsTimedOut,
  createChatState,
  markClarifyResolved,
  markApprovalResolved,
  markApprovalResolvedInSessions,
  markTurnInterrupted,
  pushUserMessage,
  stateFromHistory,
  toggleToolExpanded,
  type ChatApprovalCard,
  type ChatClarifyCard,
  type ChatMessage,
  type ChatStepStatus,
  type ChatTurnState,
} from "@/lib/chat";
import { subscribeApprovalResolved } from "@/lib/approvalEvents";
import { Markdown } from "@/components/Markdown";
import StopButton from "@/components/StopButton";
import { cn } from "@/lib/ops";

/**
 * Chat session page (batch 31, core-value page of the UI shell):
 * POST /api/chat/sessions + GET /api/chat/sessions + POST
 * /api/chat/sessions/{id}/messages (SSE: chat:delta/chat:reasoning/tool/
 * tool_result/approval_pending/done/error). Streaming typewriter rendering;
 * collapsible tool-call rows; approval cards appear inline in the message
 * stream (via the existing POST /api/approvals/{id}/approve|deny); serial per
 * session, input disabled while the agent is busy.
 *
 * Batch 41: tool output attached by server-side tool_id (§5); reasoning shown
 * collapsible (§3); ordered tool/approval step sequence (§4); post-stop busy
 * verification (§23); full approval details (§6); busy indicator restored on
 * switching back (§7); per-session model dropdown (§8).
 */

const STEP_STATUS_CLASS: Record<ChatStepStatus, string> = {
  running: "bg-sky-500/15 text-sky-600 dark:text-sky-400",
  done: "bg-[var(--vigil-ok)]/15 text-[var(--vigil-ok)]",
  failed: "bg-red-500/15 text-red-600 dark:text-red-400",
  pending: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
  approved: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  denied: "bg-red-500/15 text-red-600 dark:text-red-400",
  answered: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  timed_out: "bg-red-500/15 text-red-600 dark:text-red-400",
};

/** Reasoning collapse block (batch 41 §3): collapsed to a one-line summary by default, expand for the full text. */
function ReasoningBlock({ text }: { text: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const trimmed = text.trim();
  if (!trimmed) return null;
  const summary = trimmed.split("\n")[0].slice(0, 80) || t("chat.reasoningFallback");
  return (
    <div className="mt-2 overflow-hidden rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-xs hover:bg-black/5 dark:hover:bg-white/5"
      >
        {open ? (
          <ChevronDown className="size-3.5 shrink-0 text-[var(--vigil-muted)]" />
        ) : (
          <ChevronRight className="size-3.5 shrink-0 text-[var(--vigil-muted)]" />
        )}
        <Brain className="size-3.5 shrink-0 text-violet-500" />
        <span className="font-medium">{t("chat.reasoningToggle", { n: trimmed.length })}</span>
        <span className="ml-auto min-w-0 flex-1 truncate text-[var(--vigil-muted)]">
          {open ? "" : summary}
        </span>
      </button>
      {open && (
        <pre className="scroll-thin max-h-72 overflow-y-auto whitespace-pre-wrap break-words border-t border-[var(--vigil-border)] px-2.5 py-2 font-mono text-[11px] leading-relaxed text-[var(--vigil-text)] opacity-90">
          {trimmed}
        </pre>
      )}
    </div>
  );
}

function ToolRow({
  tool,
  stepNo,
  status,
  onToggle,
}: {
  tool: ChatMessage["tools"][number];
  stepNo: number;
  status: ChatStepStatus;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  const [reasoningOpen, setReasoningOpen] = useState(false);
  const reasoning = (tool.reasoning ?? "").trim();
  const reasoningSummary = reasoning.split("\n")[0].slice(0, 80) || t("chat.stepReasoningFallback");
  return (
    <div className="overflow-hidden rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)]">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={tool.expanded}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-xs hover:bg-black/5 dark:hover:bg-white/5"
      >
        <span className="w-4 shrink-0 text-center font-mono text-[10px] text-[var(--vigil-muted)]">
          {stepNo}
        </span>
        {tool.expanded ? (
          <ChevronDown className="size-3.5 shrink-0 text-[var(--vigil-muted)]" />
        ) : (
          <ChevronRight className="size-3.5 shrink-0 text-[var(--vigil-muted)]" />
        )}
        <Wrench className="size-3.5 shrink-0 text-sky-500" />
        <span className="font-mono font-medium">{tool.name}</span>
        <span
          className={cn(
            "ml-auto shrink-0 rounded px-1.5 py-px text-[10px]",
            STEP_STATUS_CLASS[status],
          )}
        >
          {t(`chat.stepStatus.${status}`)}
        </span>
        {tool.ok !== undefined && tool.ok === false && (
          <span className="shrink-0 text-[10px] text-[var(--vigil-error)]">✗</span>
        )}
      </button>
      {reasoning && (
        <div className="border-t border-[var(--vigil-border)]">
          <button
            type="button"
            onClick={() => setReasoningOpen((v) => !v)}
            aria-expanded={reasoningOpen}
            data-testid={`tool-reasoning-${tool.id}`}
            className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[11px] hover:bg-black/5 dark:hover:bg-white/5"
          >
            {reasoningOpen ? (
              <ChevronDown className="size-3 shrink-0 text-[var(--vigil-muted)]" />
            ) : (
              <ChevronRight className="size-3 shrink-0 text-[var(--vigil-muted)]" />
            )}
            <Brain className="size-3 shrink-0 text-violet-500" />
            <span className="font-medium">{t("chat.stepReasoningToggle", { n: reasoning.length })}</span>
            <span className="ml-auto min-w-0 flex-1 truncate text-[var(--vigil-muted)]">
              {reasoningOpen ? "" : reasoningSummary}
            </span>
          </button>
          {reasoningOpen && (
            <pre className="scroll-thin max-h-72 overflow-y-auto whitespace-pre-wrap break-words border-t border-[var(--vigil-border)] px-2.5 py-2 font-mono text-[11px] leading-relaxed text-[var(--vigil-text)] opacity-90">
              {reasoning}
            </pre>
          )}
        </div>
      )}
      {tool.expanded && (
        <div className="space-y-1.5 border-t border-[var(--vigil-border)] px-2.5 py-2">
          {tool.inputSummary && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-[var(--vigil-muted)]">{t("chat.input")}</div>
              <pre className="scroll-thin whitespace-pre-wrap break-words font-mono text-[11px] text-[var(--vigil-text)] opacity-85">
                {tool.inputSummary}
              </pre>
            </div>
          )}
          {tool.outputSummary !== undefined && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-[var(--vigil-muted)]">{t("chat.output")}</div>
              <pre className="scroll-thin max-h-64 overflow-y-auto whitespace-pre-wrap break-words font-mono text-[11px] text-[var(--vigil-text)] opacity-85">
                {tool.outputSummary || t("chat.emptyOutput")}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Approval card: scrollable long command + full description (batch 41 §6),
 * status label after approve/deny. Batch 42 §BK: shows a summary of the
 * reasoning that triggered the approval (one line, expandable, collapsed by
 * default) — a blind-approval guard so users can see why the agent wants to
 * run this command before deciding. */
function ApprovalCard({
  card,
  onResolve,
  triggerReasoning,
}: {
  card: ChatApprovalCard;
  onResolve: (card: ChatApprovalCard, status: "approved" | "denied") => void;
  triggerReasoning?: string;
}) {
  const { t } = useTranslation();
  const busy = card.status === "approved" || card.status === "denied";
  const [expanded, setExpanded] = useState(false);
  const [reasonOpen, setReasonOpen] = useState(false);
  const triggerText = (triggerReasoning ?? "").trim();
  const triggerSummary = triggerText.split("\n")[0].slice(0, 80) || t("chat.triggerReasoningFallback");
  const longCommand = (card.command ?? "").length > 80;
  const [timedOut, setTimedOut] = useState(false);
  useEffect(() => {
    const check = () => setTimedOut(approvalIsTimedOut(card));
    check();
    const timer = window.setInterval(check, 1000);
    return () => window.clearInterval(timer);
  }, [card.status, card.timeoutAt]);
  return (
    <div
      className={cn(
        "flex flex-col gap-1.5 rounded-md border px-3 py-2 text-xs",
        card.status === "approved"
          ? "border-emerald-500/50 bg-emerald-500/10"
          : card.status === "denied"
            ? "border-red-500/50 bg-red-500/10"
            : "border-amber-500/50 bg-amber-500/10",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <ShieldAlert className={cn("size-4 shrink-0", card.status === "approved" ? "text-emerald-500" : card.status === "denied" ? "text-red-500" : "text-amber-500")} />
        <span className="font-medium">{t("chat.needsApproval")}</span>
        {card.grade && <span className="rounded bg-amber-500/15 px-1.5 py-px text-[10px] text-amber-600 dark:text-amber-400">L{card.grade}</span>}
        {card.env && <span className="text-[var(--vigil-muted)]">{card.env}</span>}
        <span className="ml-auto shrink-0 text-[10px]">
          {card.status === "pending" && timedOut ? (
            <span className="text-red-500">{t("chat.approvalTimedOut")}</span>
          ) : card.status === "approved" ? (
            <span className="text-emerald-500">{t("chat.approved")}</span>
          ) : card.status === "denied" ? (
            <span className="text-red-500">{t("chat.denied")}</span>
          ) : (
            <span className="text-amber-500">{t("chat.awaitingApproval")}</span>
          )}
        </span>
      </div>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="min-w-0 text-left font-mono text-[11px]"
        title={longCommand ? t("chat.expandCommandTitle") : card.command}
      >
        <span className="block whitespace-pre-wrap break-words">
          {expanded || !longCommand ? card.command : `${card.command.slice(0, 80)}…`}
        </span>
      </button>
      {expanded && longCommand && (
        <button
          type="button"
          onClick={() => setExpanded(false)}
          className="self-start text-[10px] text-[var(--vigil-muted)] underline"
        >
          {t("common.collapse")}
        </button>
      )}
      {card.description && (
        <span className="whitespace-pre-wrap break-words text-[11px] text-[var(--vigil-muted)]">
          {card.description}
        </span>
      )}
      {triggerText && (
        <div className="overflow-hidden rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)]">
          <button
            type="button"
            onClick={() => setReasonOpen((v) => !v)}
            aria-expanded={reasonOpen}
            data-testid={`approval-reasoning-${card.approvalId}`}
            className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[11px] hover:bg-black/5 dark:hover:bg-white/5"
          >
            {reasonOpen ? (
              <ChevronDown className="size-3 shrink-0 text-[var(--vigil-muted)]" />
            ) : (
              <ChevronRight className="size-3 shrink-0 text-[var(--vigil-muted)]" />
            )}
            <Brain className="size-3 shrink-0 text-violet-500" />
            <span className="font-medium">{t("chat.triggerReasoningToggle", { n: triggerText.length })}</span>
            <span className="ml-auto min-w-0 flex-1 truncate text-[var(--vigil-muted)]">
              {reasonOpen ? "" : triggerSummary}
            </span>
          </button>
          {reasonOpen && (
            <pre className="scroll-thin max-h-64 overflow-y-auto whitespace-pre-wrap break-words border-t border-[var(--vigil-border)] px-2.5 py-2 font-mono text-[11px] leading-relaxed text-[var(--vigil-text)] opacity-90">
              {triggerText}
            </pre>
          )}
        </div>
      )}
      {card.status === "pending" && !timedOut && (
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => onResolve(card, "approved")}
            className="vigil-btn h-7 whitespace-nowrap border border-emerald-500/50 px-2 text-xs text-emerald-600 dark:text-emerald-400"
          >
            <Check className="size-3.5" /> {t("approvals.approve")}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onResolve(card, "denied")}
            className="vigil-btn h-7 whitespace-nowrap border border-red-500/50 px-2 text-xs text-red-600 dark:text-red-400"
          >
            <X className="size-3.5" /> {t("approvals.deny")}
          </button>
        </div>
      )}
    </div>
  );
}

/** Batch 49: in-stream clarify card (question + choice buttons + free text +
 * submit/cancel + timeout countdown). Not a global modal (clarify is a
 * "question", approval is a "command confirmation"). Submit → POST
 * /api/chat/sessions/{id}/clarify → card collapses and the agent turn
 * continues; timeout → "timed out, the agent decides on its own" (backend
 * timeout semantics, frontend countdown displayed in sync). */
function ClarifyCard({
  card,
  onResolve,
}: {
  card: ChatClarifyCard;
  onResolve: (card: ChatClarifyCard, answer: string | string[]) => void;
}) {
  const { t } = useTranslation();
  // error state is retryable (submit failed → buttons stay usable); answered/timed_out are terminal.
  const busy = card.status === "answered" || card.status === "timed_out";
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [custom, setCustom] = useState("");
  const [timedOut, setTimedOut] = useState(false);
  const [remaining, setRemaining] = useState<number | null>(null);
  useEffect(() => {
    const check = () => {
      setTimedOut(clarifyIsTimedOut(card));
      if (card.timeoutAt) {
        const ms = Date.parse(card.timeoutAt) - Date.now();
        setRemaining(Number.isNaN(ms) ? null : Math.max(0, Math.floor(ms / 1000)));
      } else {
        setRemaining(null);
      }
    };
    check();
    const timer = window.setInterval(check, 1000);
    return () => window.clearInterval(timer);
  }, [card.status, card.timeoutAt]);

  const expired = timedOut || card.status === "timed_out";
  const hasChoices = (card.choices?.length ?? 0) > 0;
  const toggle = (choice: string) => {
    setSelected((prev) => {
      const nextSet = new Set(prev);
      if (card.multiSelect) {
        if (nextSet.has(choice)) nextSet.delete(choice);
        else nextSet.add(choice);
      } else {
        nextSet.clear();
        nextSet.add(choice);
      }
      return nextSet;
    });
  };
  const submit = () => {
    if (busy || expired) return;
    if (card.multiSelect && hasChoices) {
      const picked = Array.from(selected);
      const customText = custom.trim();
      onResolve(card, picked.length > 0 ? picked : customText ? [customText] : []);
      return;
    }
    const customText = custom.trim();
    if (hasChoices && !customText) {
      const picked = Array.from(selected);
      onResolve(card, picked.length > 0 ? picked[0] : "");
      return;
    }
    onResolve(card, customText);
  };
  const cancel = () => {
    if (busy || expired) return;
    onResolve(card, "");
  };
  const fmt = (s: number): string => {
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
  };

  return (
    <div
      data-testid={`clarify-card-${card.clarifyId}`}
      className={cn(
        "flex flex-col gap-2 rounded-md border px-3 py-2 text-xs",
        card.status === "answered"
          ? "border-emerald-500/50 bg-emerald-500/10"
          : expired
            ? "border-red-500/50 bg-red-500/10"
            : "border-sky-500/50 bg-sky-500/10",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <HelpCircle className="size-4 shrink-0 text-sky-500" />
        <span className="font-medium">{t("chat.clarifyTitle")}</span>
        {card.status === "answered" ? (
          <span className="ml-auto text-emerald-500">{t("chat.clarifySubmitted")}</span>
        ) : expired ? (
          <span className="ml-auto text-red-500">{t("chat.clarifyTimedOut")}</span>
        ) : card.status === "error" ? (
          <span className="ml-auto text-red-500">{t("chat.clarifySubmitFailed")}</span>
        ) : (
          <span className="ml-auto shrink-0 text-[10px]">
            {remaining !== null && (
              <span className={cn(remaining <= 60 ? "text-red-500" : "text-[var(--vigil-muted)]")}>
                {t("chat.clarifyRemaining", { time: fmt(remaining) })}
              </span>
            )}
          </span>
        )}
      </div>
      <div className="whitespace-pre-wrap break-words text-[13px] text-[var(--vigil-text)]">
        {card.question}
      </div>
      {hasChoices && (
        <div className="flex flex-wrap gap-1.5">
          {(card.choices ?? []).map((choice) => {
            const on = selected.has(choice);
            return (
              <button
                key={choice}
                type="button"
                disabled={busy || expired}
                onClick={() => toggle(choice)}
                aria-pressed={on}
                className={cn(
                  "rounded border px-2 py-1 text-[11px] transition-colors disabled:opacity-50",
                  on
                    ? "border-sky-500/70 bg-sky-500/15 text-sky-600 dark:text-sky-400"
                    : "border-[var(--vigil-border)] bg-[var(--vigil-card)] text-[var(--vigil-text)] hover:bg-[var(--vigil-muted-bg)]",
                )}
              >
                {card.multiSelect && <span className="mr-1">{on ? "☑" : "☐"}</span>}
                {choice}
              </button>
            );
          })}
        </div>
      )}
      <input
        value={custom}
        onChange={(e) => setCustom(e.target.value)}
        disabled={busy || expired}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
        placeholder={hasChoices ? t("chat.clarifyOtherPlaceholder") : t("chat.clarifyInputPlaceholder")}
        data-testid={`clarify-input-${card.clarifyId}`}
        className="h-8 min-w-0 rounded border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-2 text-xs text-[var(--vigil-text)] outline-none placeholder:text-[var(--vigil-muted)]/60 disabled:opacity-50"
      />
      {card.status === "pending" && !expired && (
        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={cancel}
            className="vigil-btn h-7 border border-[var(--vigil-border)] px-2 text-xs text-[var(--vigil-muted)] disabled:opacity-50"
          >
            {t("common.cancel")}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={submit}
            data-testid={`clarify-submit-${card.clarifyId}`}
            className="vigil-btn h-7 whitespace-nowrap border border-sky-500/50 px-2 text-xs text-sky-600 dark:text-sky-400 disabled:opacity-50"
          >
            {t("chat.submit")}
          </button>
        </div>
      )}
    </div>
  );
}

function StepList({
  msg,
  onToggleTool,
  onResolveApproval,
  onResolveClarify,
}: {
  msg: ChatMessage;
  onToggleTool: (toolId: number) => void;
  onResolveApproval: (card: ChatApprovalCard, status: "approved" | "denied") => void;
  onResolveClarify: (card: ChatClarifyCard, answer: string | string[]) => void;
}) {
  const { t } = useTranslation();
  const toolById = new Map(msg.tools.map((tool) => [tool.id, tool]));
  const approvalById = new Map(msg.approvals.map((a) => [a.approvalId, a]));
  const clarifyById = new Map(msg.clarifies.map((c) => [c.clarifyId, c]));
  const steps = msg.steps;
  if (steps.length === 0) return null;

  // Batch 42 §BK: an approval's "trigger reasoning" = the per-step reasoning
  // of the closest preceding tool step (in SSE, reasoning arrives with
  // chat:tool before the approval fires inside the tool execution, so order
  // naturally holds).
  const triggerReasoningFor = (index: number): string => {
    for (let i = index - 1; i >= 0; i--) {
      const prev = steps[i];
      if (prev.kind === "tool") {
        const tool = toolById.get(Number(prev.ref));
        return tool?.reasoning ?? "";
      }
    }
    return "";
  };

  const current = steps.find((s) => s.status === "running" || s.status === "pending");
  return (
    <div data-testid="step-list" className="mt-2 space-y-1.5">
      {steps.length > 1 && (
        <div className="flex items-center gap-1.5 text-[10px] text-[var(--vigil-muted)]">
          <ListOrdered className="size-3" />
          <span>
            {t("chat.stepsTotal", { n: steps.length })}
            {current && <> · {t("chat.stepsCurrent", { status: t(`chat.stepStatus.${current.status}`) })}</>}
          </span>
        </div>
      )}
      <ol className="space-y-1.5">
        {steps.map((step, i) => {
          const no = i + 1;
          if (step.kind === "tool") {
            const tool = toolById.get(Number(step.ref));
            if (!tool) return null;
            return (
              <li key={`tool-${step.ref}`}>
                <ToolRow tool={tool} stepNo={no} status={step.status} onToggle={() => onToggleTool(tool.id)} />
              </li>
            );
          }
          if (step.kind === "approval") {
            const card = approvalById.get(String(step.ref));
            if (!card) return null;
            return (
              <li key={`approval-${step.ref}`} className="ml-3">
                <div className="flex items-center gap-1.5 text-[10px] text-[var(--vigil-muted)]">
                  <span className="w-4 text-center font-mono">{no}</span>
                  <span>{t("chat.stepApproval")}</span>
                </div>
                <ApprovalCard
                  card={card}
                  onResolve={onResolveApproval}
                  triggerReasoning={triggerReasoningFor(i)}
                />
              </li>
            );
          }
          const card = clarifyById.get(String(step.ref));
          if (!card) return null;
          return (
            <li key={`clarify-${step.ref}`} className="ml-3">
              <div className="flex items-center gap-1.5 text-[10px] text-[var(--vigil-muted)]">
                <span className="w-4 text-center font-mono">{no}</span>
                <span>clarify</span>
              </div>
              <ClarifyCard card={card} onResolve={onResolveClarify} />
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function MessageBubble({ msg, onToggleTool, onResolveApproval, onResolveClarify }: {
  msg: ChatMessage;
  onToggleTool: (toolId: number) => void;
  onResolveApproval: (card: ChatApprovalCard, status: "approved" | "denied") => void;
  onResolveClarify: (card: ChatClarifyCard, answer: string | string[]) => void;
}) {
  const { t } = useTranslation();
  if (msg.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-lg rounded-br-sm border border-[var(--vigil-border)] bg-[var(--vigil-primary)]/10 px-3 py-2">
          <div className="mb-1 flex items-center gap-1.5 text-[10px] text-[var(--vigil-muted)]">
            <User className="size-3" /> {t("chat.you")}
          </div>
          <div className="whitespace-pre-wrap break-words text-sm">{msg.content}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex justify-start">
      <div className="max-w-[92%] min-w-0 rounded-lg rounded-bl-sm border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-3 py-2 shadow-[var(--vigil-shadow)]">
        <div className="mb-1 flex items-center gap-1.5 text-[10px] text-[var(--vigil-muted)]">
          <Bot className="size-3" /> Vigil
          {msg.streaming && <Loader2 className="size-3 animate-spin text-[var(--vigil-muted)]" />}
        </div>
        {msg.interrupted && (
          <div className="mb-2 flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-600 dark:text-amber-400">
            <Square className="size-3.5 shrink-0" /> {t("chat.stopped")}
          </div>
        )}
        {msg.error && (
          <div className="flex items-center gap-2 rounded-md border border-red-500/50 bg-red-500/10 px-3 py-2 text-xs text-red-600 dark:text-red-400">
            <XCircle className="size-4 shrink-0" />
            {translateBackendMessage(msg.error, i18n.language === "en" ? "en" : "zh")}
          </div>
        )}
        <StepList
          msg={msg}
          onToggleTool={onToggleTool}
          onResolveApproval={onResolveApproval}
          onResolveClarify={onResolveClarify}
        />
        <ReasoningBlock text={msg.reasoning} />
        {msg.content ? (
          <div data-testid="assistant-content">
            <Markdown text={msg.content} />
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** Batch 41 §23: after stop, verify the registry busy flip (up to 10s); once
 * flipped, force-refetch history + keep the local "stopped" row; if not
 * flipped, surface a visible warning instead of silently lingering. */
async function verifyBusyCleared(
  sid: string,
  setStates: Dispatch<SetStateAction<Record<string, ChatTurnState>>>,
  setStopWarning: Dispatch<SetStateAction<string | null>>,
  setBusyMap?: Dispatch<SetStateAction<Record<string, boolean>>>,
): Promise<void> {
  const deadline = Date.now() + 10_000;
  try {
    while (Date.now() < deadline) {
      const resp = await api.listChatSessions();
      const s = (resp.sessions ?? []).find((x) => x.id === sid);
      if (s && !s.busy) {
        const h = await api.getChatHistory(sid);
        setStates((prev) => {
          const recovered = stateFromHistory(h.messages ?? [], false);
          return { ...prev, [sid]: markTurnInterrupted(recovered) };
        });
        setStopWarning(null);
        return;
      }
      await new Promise((r) => setTimeout(r, 2000));
    }
    setStopWarning(i18n.t("chat.busyStill"));
    // Clear the local/registry busy snapshot so input isn't disabled forever —
    // if the backend really is still busy, send is rejected with 409 busy and
    // an error shows; the user stays in control instead of silently freezing.
    setBusyMap?.((prev) => ({ ...prev, [sid]: false }));
  } catch {
    setStopWarning(i18n.t("chat.busyUnclear"));
    setBusyMap?.((prev) => ({ ...prev, [sid]: false }));
  }
}

// ── Batch 64: token usage panel (current session live + historical totals + cost) ─────────────

const USAGE_SOURCE_KEYS = new Set(["manual", "online", "builtin"]);

function formatTokens(n: number | null | undefined): string {
  const v = Number(n ?? 0);
  return Number.isFinite(v) ? v.toLocaleString() : "0";
}

/** Batch 81: compact context-usage format (37K / 131K; "—" when unavailable). */
function formatCompactTokens(n: number | null | undefined): string {
  const v = Number(n ?? 0);
  if (!Number.isFinite(v) || v < 0) return "—";
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `${Math.round(v / 1_000)}K`;
  return String(v);
}

function contextUsageLabel(u: ChatContextUsage | null | undefined): string {
  if (!u || u.used_tokens === null || u.used_tokens === undefined) return "—";
  if (u.limit_tokens) {
    return `${formatCompactTokens(u.used_tokens)} / ${formatCompactTokens(u.limit_tokens)}`;
  }
  return formatCompactTokens(u.used_tokens);
}

function formatCost(cost: number | null | undefined, currency: string | null | undefined): string | null {
  const v = Number(cost);
  if (cost === null || cost === undefined || !Number.isFinite(v)) return null;
  const symbol = currency === "cny" ? "¥" : "$";
  return i18n.t("chat.costApprox", { value: `${symbol}${v.toFixed(2)}` });
}

function localTodayKey(): string {
  const d = new Date();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

function UsagePanel({
  activeId,
  usage,
  usageLoading,
  usageError,
  analytics,
  analyticsLoading,
}: {
  activeId: string | null;
  usage: ChatUsageResponse | null;
  usageLoading: boolean;
  usageError: string | null;
  analytics: UsageAnalyticsResponse | null;
  analyticsLoading: boolean;
}) {
  const { t } = useTranslation();
  const daily = analytics?.daily ?? [];
  const todayRow = daily.find((r) => r.day === localTodayKey()) ?? daily[daily.length - 1];
  const totals = analytics?.totals;
  const price = usage?.price ?? null;
  const costText = price ? formatCost(usage?.cost ?? null, usage?.cost_currency ?? null) : null;
  const sourceLabel = price
    ? USAGE_SOURCE_KEYS.has(price.source)
      ? t(`chat.usageSource.${price.source}`)
      : price.source
    : null;

  const row = (label: string, value: string, cost?: string | null) => (
    <div className="flex items-center justify-between gap-2">
      <span className="text-[var(--vigil-muted)]">{label}</span>
      <span className="font-mono text-[var(--vigil-text)]">{value}{cost ? t("chat.parens", { value: cost }) : null}</span>
    </div>
  );

  return (
    <div data-testid="usage-panel" className="mb-3 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)] p-3 text-xs">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Coins className="size-3.5 text-[var(--vigil-muted)]" />
        <span className="font-semibold">{t("chat.usageTitle")}</span>
        <span className="text-[var(--vigil-muted)]">
          {t("chat.usageNote")}
        </span>
      </div>
      <div className="grid gap-3 md:grid-cols-3">
        <div className="space-y-1 rounded border border-[var(--vigil-border)] p-2">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium">{t("chat.usageCurrentSession")}</span>
            {usageLoading && <Loader2 className="size-3 animate-spin text-[var(--vigil-muted)]" />}
          </div>
          {!activeId ? (
            <div className="text-[var(--vigil-muted)]">{t("chat.usageNeedSession")}</div>
          ) : usageError ? (
            <div className="text-red-600 dark:text-red-400">{usageError}</div>
          ) : usage ? (
            <>
              {row(t("chat.model"), usage.model || "—")}
              {row(t("chat.inputTokens"), formatTokens(usage.input_tokens))}
              {row(t("chat.outputTokens"), formatTokens(usage.output_tokens))}
              {row(t("chat.totalTokens"), formatTokens(usage.total_tokens))}
              {costText ? (
                <div className="flex items-center justify-between gap-2 pt-1">
                  <span className="text-[var(--vigil-muted)]">{t("chat.cost")}</span>
                  <span className="font-mono text-[var(--vigil-ok)]">
                    {costText}
                    {sourceLabel ? <span className="ml-1 text-[10px] text-[var(--vigil-muted)]">{t("chat.parens", { value: sourceLabel })}</span> : null}
                  </span>
                </div>
              ) : (
                <div className="pt-1 text-[10px] text-[var(--vigil-muted)]">{t("chat.priceUnavailable")}</div>
              )}
            </>
          ) : (
            <div className="text-[var(--vigil-muted)]">{t("common.loading")}</div>
          )}
        </div>

        <div className="space-y-1 rounded border border-[var(--vigil-border)] p-2">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium">{t("chat.today")}</span>
            {analyticsLoading && <Loader2 className="size-3 animate-spin text-[var(--vigil-muted)]" />}
          </div>
          {row(t("chat.inputTokens"), formatTokens(todayRow?.input_tokens))}
          {row(t("chat.outputTokens"), formatTokens(todayRow?.output_tokens))}
          {row(t("chat.totalTokens"), formatTokens((todayRow?.input_tokens ?? 0) + (todayRow?.output_tokens ?? 0)))}
          {row(t("chat.cost"), "—", formatCost(todayRow?.estimated_cost ?? null, "usd"))}
        </div>

        <div className="space-y-1 rounded border border-[var(--vigil-border)] p-2">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium">{t("chat.last30days")}</span>
            {analyticsLoading && <Loader2 className="size-3 animate-spin text-[var(--vigil-muted)]" />}
          </div>
          {row(t("chat.inputTokens"), formatTokens(totals?.total_input))}
          {row(t("chat.outputTokens"), formatTokens(totals?.total_output))}
          {row(t("chat.totalTokens"), formatTokens((totals?.total_input ?? 0) + (totals?.total_output ?? 0)))}
          {row(t("chat.cost"), "—", formatCost(totals?.total_estimated_cost ?? null, "usd"))}
        </div>
      </div>
    </div>
  );
}

export default function ChatPage() {
  const { t, i18n } = useTranslation();
  // Batch 82: activeId persisted via URL ?sid= (survives refresh / switching
  // back to the original session; replace doesn't pollute history; the param
  // is cleared on unmount so other pages are unaffected).
  const [searchParams, setSearchParams] = useSearchParams();
  const syncSid = useCallback(
    (sid: string | null) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (sid) next.set("sid", sid);
          else next.delete("sid");
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const [sessions, setSessions] = useState<ChatSessionSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  // Batch 33: per-session state slots — each session has its own message list
  // + busy/SSE state (stored in a map keyed by sessionId). Switching pages and
  // back no longer loses messages; while A is busy you can switch to B and
  // send; switching back to A restores the scene via the history endpoint +
  // polling.
  const [states, setStates] = useState<Record<string, ChatTurnState>>({});
  // Batch 41 §7: registry busy snapshot (updated on every listChatSessions) —
  // when switching back, the "working" indicator is restored even if the local
  // slot was lost.
  const [busyMap, setBusyMap] = useState<Record<string, boolean>>({});
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [stopWarning, setStopWarning] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState(false);
  const [stopping, setStopping] = useState(false);
  // Batch 41 §8: optional model catalog + per-session selection.
  const [modelOptions, setModelOptions] = useState<ChatModelOption[]>([]);
  const [modelSelections, setModelSelections] = useState<Record<string, string>>({});
  // Batch 64: token usage panel (fetch once on open; refetch on session switch).
  const [usageOpen, setUsageOpen] = useState(false);
  const [usageData, setUsageData] = useState<ChatUsageResponse | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageError, setUsageError] = useState<string | null>(null);
  const [analytics, setAnalytics] = useState<UsageAnalyticsResponse | null>(null);
  const [analyticsLoading, setAnalyticsLoading] = useState(false);
  const abortRefs = useRef<Record<string, AbortController>>({});
  const loadedRef = useRef<Set<string>>(new Set());
  const bottomRef = useRef<HTMLDivElement>(null);

  const activeState = (activeId && states[activeId]) || createChatState();
  // Effective busy: local slot busy or registry snapshot busy (§7: indicator doesn't depend on the message stream).
  const activeBusy = activeBusyOf(activeId, states, busyMap);
  // Batch 81: current session context usage (>80% turns amber + suggests a new session).
  const activeUsage = activeId
    ? (sessions.find((s) => s.id === activeId)?.context_usage ?? null)
    : null;
  const usagePct = activeUsage?.pct ?? null;
  const usageHot = usagePct !== null && usagePct > 80;

  const trackSessions = useCallback((list: ChatSessionSummary[]) => {
    const map: Record<string, boolean> = {};
    for (const s of list) map[s.id] = Boolean(s.busy);
    setBusyMap((prev) => ({ ...prev, ...map }));
    return list;
  }, []);

  const refreshSessions = useCallback(async () => {
    try {
      const resp = await api.listChatSessions();
      if (resp.error) return [];
      const list = resp.sessions ?? [];
      trackSessions(list);
      setSessions(list);
      return list;
    } catch {
      return [];
    }
  }, [trackSessions]);

  const loadSessionHistory = useCallback(async (id: string) => {
    if (loadedRef.current.has(id)) return;
    loadedRef.current.add(id);
    try {
      const resp = await api.getChatHistory(id);
      setStates((prev) => {
        const existing = prev[id];
        if (existing && existing.messages.length > 0) return prev;
        return { ...prev, [id]: stateFromHistory(resp.messages ?? [], Boolean(resp.busy)) };
      });
      // Sync the registry busy snapshot (the history endpoint carries busy).
      setBusyMap((prev) => ({ ...prev, [id]: Boolean(resp.busy) }));
    } catch {
      loadedRef.current.delete(id);
    }
  }, []);

  const createSession = useCallback(
    async (model?: string) => {
      setError(null);
      setBusyAction(true);
      try {
        // Batch 51: model entries carry a provider (custom:<slug> etc.), submitted with creation for routing.
        const entry = model ? modelOptions.find((m) => m.id === model) : undefined;
        const resp = await api.createChatSession(model, entry?.provider);
        const sid = resp.chat_session_id;
        setActiveId(sid);
        syncSid(sid);
        loadedRef.current.add(sid);
        setStates((prev) => ({ ...prev, [sid]: createChatState() }));
        setModelSelections((prev) => ({
          ...prev,
          [sid]: resp.model ?? modelOptions.find((m) => m.default)?.id ?? "",
        }));
        await refreshSessions();
      } catch (e) {
        setError(e instanceof ApiError ? `[${e.code}] ${e.message}` : e instanceof Error ? e.message : String(e));
      } finally {
        setBusyAction(false);
      }
    },
    [refreshSessions, modelOptions, syncSid],
  );

  // Init: fetch the model catalog + list live sessions; create one if none.
  useEffect(() => {
    let alive = true;
    void api.getModels().then((resp) => {
      if (!alive) return;
      setModelOptions(resp.models ?? []);
      const def = resp.default_model ?? "";
      if (def) setModelSelections((prev) => ({ ...prev, __default: def }));
    }).catch(() => {});
    api
      .listChatSessions()
      .then(async (resp) => {
        if (!alive) return;
        const list = resp.sessions ?? [];
        trackSessions(list);
        setSessions(list);
        if (list.length > 0) {
          // Batch 82: URL sid wins when present and in the list; invalid/missing
          // → fall back to the first session and sync the URL so the address bar
          // matches the session actually viewed (silent, no error).
          const urlSid = searchParams.get("sid");
          const target =
            urlSid && list.some((s) => s.id === urlSid) ? urlSid : list[0].id;
          setActiveId(target);
          if (target !== urlSid) syncSid(target);
        } else {
          try {
            const created = await api.createChatSession();
            if (!alive) return;
            setActiveId(created.chat_session_id);
            syncSid(created.chat_session_id);
            setSessions([{ id: created.chat_session_id, title: "", created_at: "", busy: false }]);
          } catch (e) {
            if (alive) setError(e instanceof Error ? e.message : String(e));
          }
        }
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
      for (const c of Object.values(abortRefs.current)) c?.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // BUGFIX (OPS-DELTA #107): REMOVED the batch-82 "unmount → clear sid" effect.
  // It called setSearchParams in the unmount cleanup, which resolved against the
  // component's STALE location (/chat?sid=…) and replaceState'd the URL back to
  // /chat right after a nav pushState (/runbooks) — the pending navigation was
  // overwritten, so clicking any sidebar link while a session was active (URL had
  // ?sid=) appeared to do nothing. Leaving ?sid= in the URL is harmless (no other
  // page reads it) and actually helps: re-entering /chat restores the same session
  // (batch 82's original intent, via the init-time urlSid logic below).


  // Switching to a session without cache → fetch history to restore the scene (1a: messages intact across switches).
  useEffect(() => {
    if (!activeId) return;
    void loadSessionHistory(activeId);
  }, [activeId, loadSessionHistory]);

  // Batch 41 §7: refetch the registry busy immediately on activeId change/mount
  // (restores the "working" indicator on switch-back without waiting for the
  // 2s poll).
  useEffect(() => {
    if (!activeId) return;
    let alive = true;
    void refreshSessions().then((list) => {
      if (!alive) return;
      const s = (list ?? []).find((x) => x.id === activeId);
      if (s && s.busy) {
        setStates((prev) => {
          const slot = prev[activeId];
          if (slot && slot.busy) return prev;
          // Slot missing or local busy lost → restore the indicator from the
          // registry busy (keeps existing messages; the "working" visual survives
          // missing incremental stream events).
          const st = slot ? { ...slot, busy: true } : { ...createChatState(), busy: true };
          return { ...prev, [activeId]: st };
        });
      }
    });
    return () => {
      alive = false;
    };
  }, [activeId, refreshSessions]);

  // Background turn completion polling: while the active session is busy (local
  // or registry snapshot) poll the registry; on busy flip → refetch history for
  // the final content (acceptance 1b③: "working" while running, restored when
  // done).
  useEffect(() => {
    if (!activeId || !activeBusy) return;
    const timer = window.setInterval(async () => {
      try {
        const resp = await api.listChatSessions();
        trackSessions(resp.sessions ?? []);
        const s = (resp.sessions ?? []).find((x) => x.id === activeId);
        if (s && !s.busy) {
          const h = await api.getChatHistory(activeId);
          setStates((prev) => {
            const existing = prev[activeId];
            // Batch 41 §23: the local "stopped" row isn't persisted in the stop
            // scenario — after restoring history, if the original slot had an
            // interrupted marker, re-apply it so the stopped state stays visible.
            const hadInterrupted = existing?.messages.some((m) => m.interrupted) ?? false;
            const st = stateFromHistory(h.messages ?? [], false);
            return { ...prev, [activeId]: hadInterrupted ? markTurnInterrupted(st) : st };
          });
          setBusyMap((prev) => ({ ...prev, [activeId]: false }));
        } else if (s?.busy) {
          // BUGFIX (OPS-DELTA #107): still busy → poll history so a post-refresh
          // UI keeps following the run. The SSE stream is created at send time and
          // dies with the page; the old code only refetched on the busy→idle flip,
          // so after a refresh mid-run output never appeared until the turn fully
          // finished. Fingerprint the tail to skip no-op ticks (avoid re-render
          // churn + autoscroll jumps while nothing moved).
          const h = await api.getChatHistory(activeId);
          setStates((prev) => {
            const slot = prev[activeId];
            if (!slot) return prev;
            // busy:false on purpose — slot.busy must stay managed by busyMap
            // (chatInputDisabled reads slot.busy; a stuck true here would keep
            // the input disabled even after a stop-timeout clears the map).
            const fresh = stateFromHistory(h.messages ?? [], false);
            const a = slot.messages;
            const b = fresh.messages;
            if (a.length === b.length && a.length > 0) {
              const la = a[a.length - 1];
              const lb = b[b.length - 1];
              if (
                la.role === lb.role &&
                la.content === lb.content &&
                (la.steps?.length ?? 0) === (lb.steps?.length ?? 0)
              ) {
                return prev;
              }
            }
            return { ...prev, [activeId]: fresh };
          });
        }
      } catch {
        // transient network hiccup — next tick retries
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [activeId, activeBusy, trackSessions]);

  // Batch 64: panel open → fetch the current session's live tokens (refetch on
  // session switch) + historical totals (global; one fetch on open is enough).
  // Cleared on close so stale session data doesn't linger.
  useEffect(() => {
    if (!usageOpen) return;
    let alive = true;
    setUsageLoading(true);
    setUsageError(null);
    if (activeId) {
      api
        .getChatUsage(activeId)
        .then((resp) => {
          if (!alive) return;
          if (resp.error) setUsageError(`[${resp.error.code}] ${resp.error.message}`);
          else setUsageData(resp);
        })
        .catch((e: unknown) => {
          if (!alive) return;
          setUsageData(null);
          setUsageError(e instanceof ApiError ? `[${e.code}] ${e.message}` : e instanceof Error ? e.message : String(e));
        })
        .finally(() => {
          if (alive) setUsageLoading(false);
        });
    } else {
      setUsageData(null);
      setUsageLoading(false);
    }
    return () => {
      alive = false;
    };
  }, [usageOpen, activeId]);

  useEffect(() => {
    if (!usageOpen) return;
    let alive = true;
    setAnalyticsLoading(true);
    api
      .getUsageAnalytics(30)
      .then((resp) => {
        if (alive && !resp.error) setAnalytics(resp);
      })
      .catch(() => {})
      .finally(() => {
        if (alive) setAnalyticsLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [usageOpen]);

  // Auto-scroll to bottom on new content.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [activeState.messages]);

  // Batch 42 §BH: global modal / approval center resolution broadcast → the
  // matching approval cards in every session slot write back immediately
  // (approved/denied), no polling or history refetch needed.
  useEffect(
    () =>
      subscribeApprovalResolved((id, status) => {
        setStates((prev) => markApprovalResolvedInSessions(prev, id, status));
      }),
    [],
  );

  const send = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      const text = draft.trim();
      const sid = activeId;
      const st = (sid && states[sid]) || createChatState();
      if (!text || !sid || chatInputDisabled(st) || activeBusy) return;
      setDraft("");
      setError(null);
      setStopWarning(null);
      setStates((prev) => ({ ...prev, [sid]: pushUserMessage(prev[sid] ?? createChatState(), text) }));
      const ctrl = new AbortController();
      abortRefs.current[sid] = ctrl;
      try {
        await api.chatStream(sid, text, (ev) => {
          setStates((prev) => ({
            ...prev,
            [sid]: applyChatEvent(prev[sid] ?? createChatState(), {
              type: ev.type,
              data: (ev.data ?? {}) as Record<string, unknown>,
            }),
          }));
        }, ctrl.signal);
        void refreshSessions();
      } catch (err) {
        if (err instanceof Error && err.name === "AbortError") return;
        const msg = err instanceof ApiError ? `[${err.code}] ${err.message}` : err instanceof Error ? err.message : String(err);
        setStates((prev) => ({
          ...prev,
          [sid]: applyChatEvent(prev[sid] ?? createChatState(), { type: "chat:error", data: { message: msg } }),
        }));
      } finally {
        if (abortRefs.current[sid] === ctrl) delete abortRefs.current[sid];
      }
    },
    [activeId, draft, refreshSessions, states, activeBusy],
  );

  const stopTurn = useCallback(async () => {
    const sid = activeId;
    const st = (sid && states[sid]) || createChatState();
    if (!sid || !chatInputDisabled(st) || stopping) return;
    setStopping(true);
    setError(null);
    setStopWarning(null);
    try {
      await api.interruptChatSession(sid);
    } catch (err) {
      // Interrupt request failed (network/409 etc.) — surface it without
      // freezing: still abort the SSE + mark stopped locally; the background
      // turn is reset via the registry polling fallback.
      const msg = err instanceof ApiError ? `[${err.code}] ${err.message}` : err instanceof Error ? err.message : String(err);
      setError(t("chat.stopNotDelivered", { msg }));
    } finally {
      setStopping(false);
    }
    abortRefs.current[sid]?.abort();
    setStates((prev) => ({
      ...prev,
      [sid]: markTurnInterrupted(prev[sid] ?? createChatState()),
    }));
    void refreshSessions();
    // Batch 41 §23: local busy cleared; an independent verification loop checks the registry flip.
    void verifyBusyCleared(sid, setStates, setStopWarning, setBusyMap);
  }, [activeId, refreshSessions, states, stopping, t]);

  const resolveApproval = useCallback(
    async (card: ChatApprovalCard, status: "approved" | "denied") => {
      const sid = activeId;
      if (!sid) return;
      setStates((prev) => ({
        ...prev,
        [sid]: markApprovalResolved(prev[sid] ?? createChatState(), card.approvalId, status === "approved" ? "approved" : "denied"),
      }));
      try {
        if (status === "approved") await api.approveApproval(card.approvalId, "once");
        else await api.denyApproval(card.approvalId);
      } catch (err) {
        const msg = err instanceof ApiError ? `[${err.code}] ${err.message}` : err instanceof Error ? err.message : String(err);
        setError(msg);
        setStates((prev) => ({
          ...prev,
          [sid]: markApprovalResolved(prev[sid] ?? createChatState(), card.approvalId, "error"),
        }));
      }
    },
    [activeId],
  );

  // Batch 49: clarify submit → POST /api/chat/sessions/{id}/clarify; close the
  // card locally first (disable while submitting to prevent double clicks),
  // roll back to error (retryable) on failure.
  const resolveClarify = useCallback(
    async (card: ChatClarifyCard, answer: string | string[]) => {
      const sid = activeId;
      if (!sid) return;
      setStates((prev) => ({
        ...prev,
        [sid]: markClarifyResolved(prev[sid] ?? createChatState(), card.clarifyId, "answered"),
      }));
      try {
        await api.answerChatClarify(sid, answer);
      } catch (err) {
        const apiErr = err instanceof ApiError ? err : null;
        // Timed out → card shows "timed out"; nothing pending (turn already wrapped) → treat as submitted.
        const status: "timed_out" | "answered" | "error" =
          apiErr?.code === "clarify_timed_out"
            ? "timed_out"
            : apiErr?.code === "no_pending_clarify"
              ? "answered"
              : "error";
        if (status === "error") {
          const msg = err instanceof ApiError ? `[${err.code}] ${err.message}` : err instanceof Error ? err.message : String(err);
          setError(msg);
        }
        setStates((prev) => ({
          ...prev,
          [sid]: markClarifyResolved(prev[sid] ?? createChatState(), card.clarifyId, status),
        }));
      }
    },
    [activeId],
  );

  const switchSession = useCallback(
    (id: string) => {
      if (id === activeId) return;
      // Switching away: abort the old session's SSE (the background turn keeps running; polling restores the scene on switch-back).
      if (activeId) abortRefs.current[activeId]?.abort();
      setActiveId(id);
      syncSid(id);
      setError(null);
      setStopWarning(null);
    },
    [activeId, syncSid],
  );

  const toggleTool = useCallback(
    (toolId: number) => {
      if (!activeId) return;
      setStates((prev) => ({
        ...prev,
        [activeId]: toggleToolExpanded(prev[activeId] ?? createChatState(), toolId),
      }));
    },
    [activeId],
  );

  // Batch 41 §8: switch the session model (per-session, applies to new messages). Batch 51: provider routing submitted along with the model.
  const changeSessionModel = useCallback(
    async (sid: string, model: string) => {
      if (!sid || !model || modelSelections[sid] === model) return;
      setModelSelections((prev) => ({ ...prev, [sid]: model }));
      try {
        const entry = modelOptions.find((m) => m.id === model);
        const resp = await api.setChatSessionModel(sid, model, entry?.provider);
        setModelSelections((prev) => ({ ...prev, [sid]: resp.model }));
        setSessions((prev) => prev.map((s) => (s.id === sid ? { ...s, model: resp.model } : s)));
      } catch (e) {
        const msg = e instanceof ApiError ? `[${e.code}] ${e.message}` : e instanceof Error ? e.message : String(e);
        setError(t("chat.modelSwitchFailed", { msg }));
        // Roll back to the session's original model.
        const s = sessions.find((x) => x.id === sid);
        setModelSelections((prev) => ({ ...prev, [sid]: s?.model ?? modelSelections.__default ?? "" }));
      }
    },
    [modelSelections, sessions, modelOptions, t],
  );

  const disabled = chatInputDisabled(activeState) || activeBusy || !activeId || busyAction;
  const lastMsg = activeState.messages[activeState.messages.length - 1];
  // Stop already clicked (last item is the local "stopped" row): hide the stop button so an inert button doesn't mislead.
  const stopIssued = Boolean(lastMsg?.interrupted);
  // BUGFIX (OPS-DELTA #107): pending approval/clarify cards are ALSO surfaced at
  // the bottom of the message stream ("arrive like a new message"). Without this,
  // a card rendered inside a long turn's step list (top of the message) sits far
  // above the viewport while the agent's latest output grows at the bottom — the
  // card looked "pinned to an old position". Resolving updates both copies via the
  // shared state (steps list keeps its slot; the floating copy disappears).
  const pendingApprovals = activeState.messages
    .flatMap((m) => m.approvals ?? [])
    .filter((c) => c.status === "pending");
  const pendingClarifies = activeState.messages
    .flatMap((m) => m.clarifies ?? [])
    .filter((c) => c.status === "pending");
  const hasPendingCards = pendingApprovals.length + pendingClarifies.length > 0;
  const activeModel =
    modelSelections[activeId ?? ""] ??
    sessions.find((s) => s.id === activeId)?.model ??
    modelSelections.__default ??
    "";
  // Batch 51: dropdown grouped by provider (custom:<slug> / provider name / default).
  const groupedModels = useMemo(() => {
    const groups = new Map<string, ChatModelOption[]>();
    for (const m of modelOptions) {
      const g = m.provider || t("chat.providerFallback");
      const list = groups.get(g);
      if (list) list.push(m);
      else groups.set(g, [m]);
    }
    return Array.from(groups.entries());
  }, [modelOptions, t]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Toolbar: session management */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-2">
          <TerminalSquare className="size-5 text-[var(--vigil-muted)]" />
          <h1 className="text-lg font-semibold">Chat</h1>
          <span className="hidden text-xs text-[var(--vigil-muted)] md:inline">{t("chat.headerTagline")}</span>
        </div>

        <div className="ml-auto flex flex-wrap items-center gap-2">
          <select
            value={activeId ?? ""}
            onChange={(e) => switchSession(e.target.value)}
            title={t("chat.sessionSwitchTitle")}
            className="h-8 max-w-[220px] rounded border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-2 font-mono text-xs text-[var(--vigil-text)] outline-none"
          >
            {sessions.length === 0 && <option value="">{t("chat.noSessions")}</option>}
            {sessions.map((s) => (
              <option key={s.id} value={s.id}>
                {s.title || s.id}
              </option>
            ))}
          </select>
          {activeUsage && (
            <span
              title={
                usagePct !== null
                  ? t("chat.contextUsed", { pct: usagePct, hot: usageHot ? t("chat.contextHotSuffix") : "" })
                  : t("chat.contextUnknown")
              }
              className={`inline-flex h-8 items-center rounded border px-2 font-mono text-[11px] ${
                usageHot
                  ? "border-orange-500/60 bg-orange-500/10 text-orange-600 dark:text-orange-400"
                  : "border-[var(--vigil-border)] bg-[var(--vigil-card)] text-[var(--vigil-muted)]"
              }`}
            >
              {contextUsageLabel(activeUsage)}
            </span>
          )}
          {usageHot && (
            <button
              type="button"
              onClick={() => void createSession(modelSelections[activeId ?? ""] ?? undefined)}
              disabled={busyAction}
              title={t("chat.contextHotTitle")}
              className="vigil-btn h-8 whitespace-nowrap border border-orange-500/60 bg-orange-500/10 text-xs text-orange-600 hover:bg-orange-500/20 dark:text-orange-400"
            >
              <MessageSquarePlus className="size-3.5" /> {t("chat.suggestNew")}
            </button>
          )}
          <button
            type="button"
            onClick={() => setUsageOpen((v) => !v)}
            title={t("chat.usageBtnTitle")}
            aria-pressed={usageOpen}
            className={`vigil-btn h-8 whitespace-nowrap border border-[var(--vigil-border)] text-xs ${
              usageOpen ? "bg-[var(--vigil-border)]/25" : ""
            }`}
          >
            <Coins className="size-3.5" /> {t("chat.usageTitle")}
          </button>
          <button
            type="button"
            onClick={() => void createSession(modelSelections[activeId ?? ""] ?? undefined)}
            disabled={busyAction}
            className="vigil-btn h-8 whitespace-nowrap border border-[var(--vigil-border)] text-xs"
          >
            <MessageSquarePlus className="size-3.5" /> {t("chat.newSession")}
          </button>
        </div>
      </div>

      {usageOpen && (
        <UsagePanel
          activeId={activeId}
          usage={usageData}
          usageLoading={usageLoading}
          usageError={usageError}
          analytics={analytics}
          analyticsLoading={analyticsLoading}
        />
      )}

      {error && (
        <div className="mb-2 flex items-center gap-2 rounded-md border border-red-500/50 bg-red-500/10 px-3 py-2 text-xs text-red-600 dark:text-red-400">
          <XCircle className="size-4 shrink-0" />
          <span className="min-w-0 flex-1">{translateBackendMessage(error, i18n.language === "en" ? "en" : "zh")}</span>
          <button type="button" onClick={() => setError(null)} aria-label={t("chat.closeError")} className="text-[var(--vigil-muted)] hover:text-[var(--vigil-text)]">
            <X className="size-3.5" />
          </button>
        </div>
      )}

      {stopWarning && (
        <div className="mb-2 flex items-center gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-xs text-amber-600 dark:text-amber-400">
          <ShieldAlert className="size-4 shrink-0" />
          <span className="min-w-0 flex-1">{stopWarning}</span>
          <button
            type="button"
            onClick={() => {
              const sid = activeId;
              if (sid) void verifyBusyCleared(sid, setStates, setStopWarning, setBusyMap);
            }}
            className="shrink-0 underline"
          >
            {t("chat.retry")}
          </button>
        </div>
      )}

      {activeState.contextWarning && (
        <div className="mb-2 flex items-center gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-xs text-amber-600 dark:text-amber-400">
          <ShieldAlert className="size-4 shrink-0" />
          <span className="min-w-0 flex-1">{activeState.contextWarning}</span>
          <button
            type="button"
            onClick={() => void createSession(modelSelections[activeId ?? ""] ?? undefined)}
            disabled={busyAction}
            className="shrink-0 underline"
          >
            {t("chat.newSessionShort")}
          </button>
        </div>
      )}

      {/* Message list */}
      <div className="scroll-thin min-h-0 flex-1 space-y-3 overflow-y-auto rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-bg)] p-3">
        {activeState.messages.length === 0 && (
          <div className="flex h-full min-h-[200px] flex-col items-center justify-center gap-2 text-sm text-[var(--vigil-muted)]">
            <Bot className="size-8 opacity-60" />
            {activeBusy ? (
              <span className="flex items-center gap-2">
                <Loader2 className="size-4 animate-spin" /> {t("chat.busyIndicator")}
              </span>
            ) : activeId ? (
              t("chat.emptyStart")
            ) : (
              t("chat.emptyCreate")
            )}
          </div>
        )}
        {activeState.messages.map((m) => (
          <MessageBubble
            key={m.id}
            msg={m}
            onToggleTool={toggleTool}
            onResolveApproval={resolveApproval}
            onResolveClarify={resolveClarify}
          />
        ))}
        {hasPendingCards && (
          <div className="space-y-2 border-t border-dashed border-amber-500/40 pt-2" data-testid="pending-cards-float">
            {pendingApprovals.map((card) => (
              <ApprovalCard key={`float-${card.approvalId}`} card={card} onResolve={resolveApproval} />
            ))}
            {pendingClarifies.map((card) => (
              <ClarifyCard key={`float-${card.clarifyId}`} card={card} onResolve={resolveClarify} />
            ))}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input row */}
      <form onSubmit={(e) => void send(e)} className="mt-2 shrink-0">
        {modelOptions.length > 0 && activeId && (
          <div className="mb-1.5 flex items-center gap-2">
            <span className="text-[10px] text-[var(--vigil-muted)]">{t("chat.model")}</span>
            <select
              value={activeModel}
              onChange={(e) => void changeSessionModel(activeId, e.target.value)}
              disabled={activeBusy || busyAction}
              title={t("chat.modelSelectTitle")}
              aria-label={t("chat.modelSelectAria")}
              className="h-7 max-w-[320px] rounded border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-2 font-mono text-[11px] text-[var(--vigil-text)] outline-none disabled:opacity-60"
            >
              {groupedModels.map(([group, items]) => (
                <optgroup key={group} label={group}>
                  {items.map((m) => (
                    <option key={`${group}::${m.id}`} value={m.id}>
                      {m.name}
                      {m.default ? t("chat.defaultSuffix") : ""}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            <span className="min-w-0 flex-1 truncate text-[10px] text-[var(--vigil-muted)]">
              {modelOptions.find((m) => m.id === activeModel)?.description || ""}
            </span>
          </div>
        )}
        <div className="flex items-center gap-2 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-3 py-2">
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={disabled ? (busyAction ? t("chat.placeholderCreating") : t("chat.placeholderBusy")) : t("chat.placeholder")}
            disabled={disabled}
            spellCheck={false}
            className="h-9 min-w-0 flex-1 bg-transparent text-sm text-[var(--vigil-text)] outline-none placeholder:text-[var(--vigil-muted)]/60 disabled:opacity-60"
          />
          {(activeBusy || chatInputDisabled(activeState)) && (
            <span className="hidden shrink-0 items-center gap-1.5 text-xs text-[var(--vigil-muted)] sm:inline-flex">
              <Loader2 className="size-3.5 animate-spin" /> {t("chat.busyIndicator")}
            </span>
          )}
          {activeBusy && !busyAction && !stopIssued && (
            <StopButton stopping={stopping} onStop={() => void stopTurn()} />
          )}
          <button
            type="submit"
            disabled={disabled || !draft.trim()}
            aria-label={t("chat.send")}
            className="vigil-btn vigil-btn-primary h-8 shrink-0 px-3 text-sm"
          >
            <Send className="size-3.5" /> {t("chat.send")}
          </button>
        </div>
      </form>
    </div>
  );
}

/** Effective busy = local slot busy OR registry snapshot busy (§7: indicator never lost). */
function activeBusyOf(
  activeId: string | null,
  states: Record<string, ChatTurnState>,
  busyMap: Record<string, boolean>,
): boolean {
  if (!activeId) return false;
  return Boolean((states[activeId] && states[activeId].busy) || busyMap[activeId]);
}
