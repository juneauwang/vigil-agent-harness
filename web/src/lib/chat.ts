/**
 * Chat page pure logic (batch 31) — SSE events → message-list state machine.
 *
 * Extracted from the component into pure functions for node-env unit tests
 * (no jsdom/testing-library):
 *   - applyChatEvent(): merges chat:delta/chat:reasoning/tool/tool_result/
 *     approval_pending/done/error increments into the message list
 *   - chatInputDisabled(): input disabled while the agent is busy (busy
 *     semantics mirror the backend 409)
 *   - markApprovalResolved(): approval-card local state (backfilled after
 *     approve/deny)
 *   - stateFromHistory(): batch 33 history endpoint → session state (scene
 *     restore on switch-back)
 * Batch 41: tool output attaches by the server's unique tool_id (zero
 * mis-attachment for parallel same-named tools); tools/approvals inside an
 * assistant message are chained into an ordered step sequence by event
 * arrival order.
 * Credential red line: tool input/output summaries only show server-redacted
 * content; the frontend does no further processing.
 */

import type { ChatHistoryMessage } from "./api";
import i18n from "@/i18n";

export type ChatStepKind = "tool" | "approval" | "clarify";
export type ChatStepStatus =
  | "running"
  | "done"
  | "failed"
  | "pending"
  | "approved"
  | "denied"
  | "answered"
  | "timed_out";

/** Ordered steps: tool calls and approval cards chained ①②③… by event arrival
 * order; ref points at an entry in the tools/approvals arrays. */
export interface ChatStep {
  kind: ChatStepKind;
  /** tool step = ChatToolEvent.id; approval step = ChatApprovalCard.approvalId. */
  ref: number | string;
  status: ChatStepStatus;
}

export interface ChatToolEvent {
  /** Local auto-increment id (render key + step references). */
  id: number;
  /** Server-unique id (model tool_call id; empty string = legacy server / fallback attach by name). */
  toolId: string;
  name: string;
  inputSummary: string;
  outputSummary?: string;
  ok?: boolean;
  expanded: boolean;
  /** Batch 42 §BJ: reasoning that led to this tool call (attributed
   * incrementally via chat:reasoning.tool_id; history structured
   * reasoning.steps backfilled by tool_id). Empty string = unattributed. */
  reasoning: string;
}

export type ChatApprovalStatus = "pending" | "approved" | "denied" | "error";

export interface ChatApprovalCard {
  approvalId: string;
  command: string;
  description: string;
  env: string;
  grade?: string;
  timeoutAt?: string | null;
  status: ChatApprovalStatus;
}

/** Batch 49: in-stream clarify card (question + choices + free text, not a global modal). */
export type ChatClarifyStatus = "pending" | "answered" | "timed_out" | "error";

export interface ChatClarifyCard {
  clarifyId: string;
  question: string;
  choices: string[] | null;
  multiSelect: boolean;
  timeoutAt?: string | null;
  status: ChatClarifyStatus;
}

export interface ChatMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  /** Batch 41: reasoning plain text (merged incrementally from chat:reasoning; carried back by the history endpoint). */
  reasoning: string;
  streaming?: boolean;
  tools: ChatToolEvent[];
  approvals: ChatApprovalCard[];
  /** Batch 49: clarify cards (rendered in-stream, alongside approval cards). */
  clarifies: ChatClarifyCard[];
  /** Batch 41: ordered step sequence of tools/approvals by arrival order. */
  steps: ChatStep[];
  error?: string;
  /** Batch 36: local status row after the user clicks "stop" (not persisted; gone on session switch / history refetch). */
  interrupted?: boolean;
}

export interface ChatTurnState {
  messages: ChatMessage[];
  busy: boolean;
  nextId: number;
  activeMessageId: number | null;
  /** Batch 81: engine-level context >=80% notice (chat:context_warning; cleared on a new turn). */
  contextWarning: string | null;
}

/** Batch 38 §AW: whether an approval card has timed out (timeout_at passed and still pending). */
export function approvalIsTimedOut(
  card: Pick<ChatApprovalCard, "status" | "timeoutAt">,
  now: number = Date.now(),
): boolean {
  if (card.status !== "pending" || !card.timeoutAt) return false;
  const deadline = Date.parse(card.timeoutAt);
  if (Number.isNaN(deadline)) return false;
  return now >= deadline;
}

/** Batch 49: whether a clarify card has timed out (timeout_at passed and still pending). */
export function clarifyIsTimedOut(
  card: Pick<ChatClarifyCard, "status" | "timeoutAt">,
  now: number = Date.now(),
): boolean {
  if (card.status !== "pending" || !card.timeoutAt) return false;
  const deadline = Date.parse(card.timeoutAt);
  if (Number.isNaN(deadline)) return false;
  return now >= deadline;
}

export function createChatState(): ChatTurnState {
  return { messages: [], busy: false, nextId: 1, activeMessageId: null, contextWarning: null };
}

/** The event currently receiving increments (chat:delta merge target). */
function activeAssistant(state: ChatTurnState): ChatMessage | null {
  if (state.activeMessageId === null) return null;
  const msg = state.messages.find((m) => m.id === state.activeMessageId);
  return msg && msg.role === "assistant" ? msg : null;
}

function lastToolEvent(msg: ChatMessage, name: string): ChatToolEvent | undefined {
  for (let i = msg.tools.length - 1; i >= 0; i--) {
    const t = msg.tools[i];
    if (t.name === name && t.outputSummary === undefined) return t;
  }
  return undefined;
}

/** Replace the message's tools/approvals with fresh objects (applyChatEvent
 * branches mutate the copy so nested fields of old state references stay
 * untouched). */
function cloneMessage(msg: ChatMessage): ChatMessage {
  return {
    ...msg,
    tools: msg.tools.map((t) => ({ ...t })),
    approvals: msg.approvals.map((a) => ({ ...a, timeoutAt: a.timeoutAt ?? null })),
    clarifies: msg.clarifies.map((c) => ({ ...c, timeoutAt: c.timeoutAt ?? null })),
    steps: msg.steps.map((s) => ({ ...s })),
  };
}

/** Read-only step lookup returning a new steps array (original array when not found). */
function withStepStatus(
  msg: ChatMessage,
  ref: number | string,
  status: ChatStepStatus,
): ChatStep[] {
  let changed = false;
  const steps = msg.steps.map((s) => {
    if (s.ref === ref && s.status !== status) {
      changed = true;
      return { ...s, status };
    }
    return s;
  });
  return changed ? steps : msg.steps;
}

export interface ChatEvent {
  type: string;
  data: Record<string, unknown>;
}

export function applyChatEvent(state: ChatTurnState, ev: ChatEvent): ChatTurnState {
  const data = ev.data ?? {};
  const next: ChatTurnState = {
    ...state,
    messages: state.messages.map((m) => cloneMessage(m)),
  };

  if (ev.type === "chat:delta") {
    const text = String(data.text ?? "");
    const active = activeAssistant(next);
    if (active) {
      active.content += text;
      active.streaming = true;
    } else {
      next.activeMessageId = next.nextId;
      next.nextId += 1;
      next.messages.push({
        id: next.activeMessageId,
        role: "assistant",
        content: text,
        reasoning: "",
        streaming: true,
        tools: [],
        approvals: [],
        clarifies: [],
        steps: [],
      });
    }
    return next;
  }

  // Batch 41 §3: reasoning increments merge into the active message's
  // reasoning field (if no active message, create one first — reasoning
  // usually arrives before the content).
  // Batch 42 §BJ: reasoning carrying a tool_id is attributed to that tool row
  // (per-step reasoning); fall back to message level when the tool can't be
  // found (legacy server / reasoning arriving before chat:tool edge case).
  if (ev.type === "chat:reasoning") {
    const text = String(data.text ?? "");
    const toolId = data.tool_id != null ? String(data.tool_id) : "";
    const active = activeAssistant(next);
    if (active) {
      active.streaming = true;
      if (toolId) {
        const tool = active.tools.find((t) => t.toolId === toolId);
        if (tool) {
          tool.reasoning += text;
          return next;
        }
      }
      active.reasoning += text;
    } else {
      next.activeMessageId = next.nextId;
      next.nextId += 1;
      next.messages.push({
        id: next.activeMessageId,
        role: "assistant",
        content: "",
        reasoning: text,
        streaming: true,
        tools: [],
        approvals: [],
        clarifies: [],
        steps: [],
      });
    }
    return next;
  }

  if (ev.type === "chat:tool") {
    const active = activeAssistant(next);
    const name = String(data.name ?? "");
    const summary = String(data.input_summary ?? "");
    const toolId = data.tool_id != null ? String(data.tool_id) : "";
    if (active) {
      const localId = next.nextId++;
      active.tools.push({ id: localId, toolId, name, inputSummary: summary, expanded: false, reasoning: "" });
      active.steps.push({ kind: "tool", ref: localId, status: "running" });
    } else {
      next.activeMessageId = next.nextId;
      next.nextId += 1;
      const localId = next.nextId++;
      next.messages.push({
        id: next.activeMessageId,
        role: "assistant",
        content: "",
        reasoning: "",
        streaming: true,
        tools: [{ id: localId, toolId, name, inputSummary: summary, expanded: false, reasoning: "" }],
        approvals: [],
        clarifies: [],
        steps: [{ kind: "tool", ref: localId, status: "running" }],
      });
    }
    return next;
  }

  if (ev.type === "chat:tool_result") {
    const active = activeAssistant(next);
    const name = String(data.name ?? "");
    const summary = String(data.output_summary ?? "");
    const ok = Boolean(data.ok ?? true);
    if (active) {
      const toolId = data.tool_id != null ? String(data.tool_id) : "";
      // Batch 41 §5: attach by the server's unique tool_id first; when a legacy
      // server sends no id, fall back to "last same-named tool without a
      // result" (preserves old behavior).
      const tool = toolId
        ? active.tools.find((t) => t.toolId === toolId)
        : lastToolEvent(active, name);
      if (tool) {
        tool.outputSummary = summary;
        tool.ok = ok;
        active.steps = withStepStatus(active, tool.id, ok ? "done" : "failed");
      }
    }
    return next;
  }

  if (ev.type === "chat:approval_pending") {
    const active = activeAssistant(next);
    const card: ChatApprovalCard = {
      approvalId: String(data.approval_id ?? ""),
      command: String(data.command ?? ""),
      description: String(data.description ?? ""),
      env: String(data.env ?? ""),
      grade: data.grade != null ? String(data.grade) : undefined,
      timeoutAt: data.timeout_at != null ? String(data.timeout_at) : null,
      status: "pending",
    };
    if (active) {
      active.approvals.push(card);
      active.steps.push({ kind: "approval", ref: card.approvalId, status: "pending" });
    } else {
      next.activeMessageId = next.nextId;
      next.nextId += 1;
      next.messages.push({
        id: next.activeMessageId,
        role: "assistant",
        content: "",
        reasoning: "",
        streaming: true,
        tools: [],
        approvals: [card],
        clarifies: [],
        steps: [{ kind: "approval", ref: card.approvalId, status: "pending" }],
      });
    }
    return next;
  }

  // Batch 49: LLM calls clarify → attach a question card in-stream (not a global modal; sits alongside approval cards).
  if (ev.type === "chat:clarify_pending") {
    const active = activeAssistant(next);
    const card: ChatClarifyCard = {
      clarifyId: String(data.clarify_id ?? ""),
      question: String(data.question ?? ""),
      choices: Array.isArray(data.choices) ? data.choices.map(String) : null,
      multiSelect: Boolean(data.multi_select),
      timeoutAt: data.timeout_at != null ? String(data.timeout_at) : null,
      status: "pending",
    };
    if (active) {
      active.clarifies.push(card);
      active.steps.push({ kind: "clarify", ref: card.clarifyId, status: "pending" });
    } else {
      next.activeMessageId = next.nextId;
      next.nextId += 1;
      next.messages.push({
        id: next.activeMessageId,
        role: "assistant",
        content: "",
        reasoning: "",
        streaming: true,
        tools: [],
        approvals: [],
        clarifies: [card],
        steps: [{ kind: "clarify", ref: card.clarifyId, status: "pending" }],
      });
    }
    return next;
  }

  if (ev.type === "chat:done") {
    const finalText = String(data.final_response ?? "");
    const active = activeAssistant(next);
    if (active) {
      active.content = finalText;
      active.streaming = false;
      // Turn finished: close out clarify cards still pending (the agent already
      // continued with the answer/timeout) — precise display is owned by the
      // card's own countdown/answer write-back.
      for (const c of active.clarifies) {
        if (c.status === "pending") c.status = clarifyIsTimedOut(c) ? "timed_out" : "answered";
      }
    }
    next.busy = false;
    next.activeMessageId = null;
    return next;
  }

  if (ev.type === "chat:error") {
    const msg = String(data.message ?? i18n.t("lib.chatErrorFallback"));
    const active = activeAssistant(next);
    if (active) {
      active.error = msg;
      active.streaming = false;
    } else {
      next.messages.push({
        id: next.nextId++,
        role: "assistant",
        content: "",
        reasoning: "",
        tools: [],
        approvals: [],
        clarifies: [],
        steps: [],
        error: msg,
      });
    }
    next.busy = false;
    next.activeMessageId = null;
    return next;
  }

  // Batch 81: engine-level proactive notice when context usage >=80% (doesn't
  // block the stream; banner + "start a new session" button until cleared on
  // the next turn).
  if (ev.type === "chat:context_warning") {
    next.contextWarning = String(data.message ?? "");
    return next;
  }

  return next;
}

/** Enqueue a user message (bubble renders immediately on send). */
export function pushUserMessage(state: ChatTurnState, text: string): ChatTurnState {
  return {
    ...state,
    busy: true,
    activeMessageId: null,
    contextWarning: null,
    nextId: state.nextId + 1,
    messages: [
      ...state.messages,
      {
        id: state.nextId,
        role: "user",
        content: text,
        reasoning: "",
        tools: [],
        approvals: [],
        clarifies: [],
        steps: [],
      },
    ],
  };
}

export function chatInputDisabled(state: ChatTurnState): boolean {
  return state.busy;
}

/** Batch 36: local state close-out after the user clicks "stop" — busy cleared + a "stopped" status row in the message area.

 * The server-side turn is truly interrupted via /interrupt (still finishing in
 * the background; busy is reset via the registry polling fallback); this only
 * provides immediate local feedback: a new message can be sent right away (the
 * backend 409 guards extreme races).
 */
export function markTurnInterrupted(state: ChatTurnState): ChatTurnState {
  const last = state.messages[state.messages.length - 1];
  // Idempotent: don't append a duplicate when the last item is already the local "stopped" row (re-applied after history restore via polling).
  if (last?.interrupted) return { ...state, busy: false, activeMessageId: null };
  return {
    ...state,
    busy: false,
    activeMessageId: null,
    nextId: state.nextId + 1,
    messages: [
      ...state.messages,
      {
        id: state.nextId,
        role: "assistant",
        content: "",
        reasoning: "",
        streaming: false,
        tools: [],
        approvals: [],
        clarifies: [],
        steps: [],
        interrupted: true,
      },
    ],
  };
}

export function markApprovalResolved(
  state: ChatTurnState,
  approvalId: string,
  status: ChatApprovalStatus,
): ChatTurnState {
  return {
    ...state,
    messages: state.messages.map((m) => {
      const clone = cloneMessage(m);
      return {
        ...clone,
        approvals: clone.approvals.map((a) =>
          a.approvalId === approvalId ? { ...a, status } : a,
        ),
        steps: withStepStatus(
          clone,
          approvalId,
          status === "approved" ? "approved" : status === "denied" ? "denied" : "failed",
        ),
      };
    }),
  };
}

/** Batch 49: clarify card local close-out (submit success → answered; submit
 * failure → error; countdown expiry → timed_out). Step status kept in sync. */
export function markClarifyResolved(
  state: ChatTurnState,
  clarifyId: string,
  status: ChatClarifyStatus,
): ChatTurnState {
  return {
    ...state,
    messages: state.messages.map((m) => {
      const clone = cloneMessage(m);
      return {
        ...clone,
        clarifies: clone.clarifies.map((c) =>
          c.clarifyId === clarifyId ? { ...c, status } : c,
        ),
        steps: withStepStatus(
          clone,
          clarifyId,
          status === "answered" ? "answered" : status === "timed_out" ? "timed_out" : "failed",
        ),
      };
    }),
  };
}

export function toggleToolExpanded(state: ChatTurnState, toolId: number): ChatTurnState {
  return {
    ...state,
    messages: state.messages.map((m) => ({
      ...m,
      tools: m.tools.map((t) => (t.id === toolId ? { ...t, expanded: !t.expanded } : t)),
    })),
  };
}

/** History messages → session state (scene restore on switch-back/page switch; batch 33).
 *
 * Server messages carry stable row ids; renumber them to local auto-increment
 * here (avoids colliding with the streaming renderer's nextId); tools have
 * already been folded into the assistant bubble by the server (incl.
 * tool_id/reasoning).
 * Batch 42 §BJ: history reasoning accepts both a plain string (message level)
 * and structured {steps:[{tool_id,text}]} (attached back to the matching tool
 * row's per-step reasoning by tool_id).
 * busy comes from the registry (a running session shows "working"; input is
 * disabled until the background turn completes).
 */
export function stateFromHistory(
  history: ChatHistoryMessage[],
  busy: boolean,
): ChatTurnState {
  let nextId = 1;
  const messages: ChatMessage[] = (history ?? []).map((m) => {
    const id = nextId++;
    if (m.role === "user") {
      return {
        id,
        role: "user",
        content: m.content ?? "",
        reasoning: "",
        tools: [],
        approvals: [],
        clarifies: [],
        steps: [],
      };
    }
    const rawReasoning = m.reasoning ?? "";
    const reasoningSteps =
      typeof rawReasoning === "object" && Array.isArray(rawReasoning?.steps)
        ? rawReasoning.steps
        : [];
    const tools: ChatToolEvent[] = (m.tools ?? []).map((t) => {
      const toolId = t.tool_id != null ? String(t.tool_id) : "";
      const step = reasoningSteps.find(
        (st) => st.tool_id != null && String(st.tool_id) === toolId,
      );
      return {
        id: nextId++,
        toolId,
        name: t.name,
        inputSummary: t.input_summary ?? "",
        outputSummary: t.output_summary ?? undefined,
        ok: t.ok ?? undefined,
        expanded: false,
        reasoning: step?.text ?? "",
      };
    });
    // Batch 82: history approval/clarify card restore (the server folded them
    // into this bubble; state follows the persisted snapshot + server
    // overrides, the frontend only normalizes types + syncs steps).
    const approvals: ChatApprovalCard[] = (m.approvals ?? []).map((a) => ({
      approvalId: String(a.approval_id ?? ""),
      command: String(a.command ?? ""),
      description: String(a.description ?? ""),
      env: String(a.env ?? ""),
      grade: a.grade != null ? String(a.grade) : undefined,
      timeoutAt: a.timeout_at != null ? String(a.timeout_at) : null,
      status:
        a.status === "approved" || a.status === "denied" || a.status === "error"
          ? a.status
          : "pending",
    }));
    const clarifies: ChatClarifyCard[] = (m.clarifies ?? []).map((c) => ({
      clarifyId: String(c.clarify_id ?? ""),
      question: String(c.question ?? ""),
      choices: Array.isArray(c.choices) ? c.choices.map(String) : null,
      multiSelect: Boolean(c.multi_select),
      timeoutAt: c.timeout_at != null ? String(c.timeout_at) : null,
      status:
        c.status === "answered" || c.status === "timed_out" || c.status === "error"
          ? c.status
          : "pending",
    }));
    return {
      id,
      role: "assistant",
      content: m.content ?? "",
      reasoning: typeof rawReasoning === "string" ? rawReasoning : "",
      streaming: false,
      tools,
      approvals,
      clarifies,
      steps: [
        ...tools.map((t) => ({
          kind: "tool" as const,
          ref: t.id,
          status: t.ok === false ? ("failed" as const) : ("done" as const),
        })),
        ...approvals.map((a) => ({
          kind: "approval" as const,
          ref: a.approvalId,
          status: a.status === "approved"
            ? ("approved" as const)
            : a.status === "denied"
              ? ("denied" as const)
              : a.status === "error"
                ? ("failed" as const)
                : ("pending" as const),
        })),
        ...clarifies.map((c) => ({
          kind: "clarify" as const,
          ref: c.clarifyId,
          status: c.status === "answered"
            ? ("answered" as const)
            : c.status === "timed_out"
              ? ("timed_out" as const)
              : c.status === "error"
                ? ("failed" as const)
                : ("pending" as const),
        })),
      ],
    };
  });
  return { messages, busy: Boolean(busy), nextId, activeMessageId: null, contextWarning: null };
}

/** Batch 42 §BH: cross-session approval resolution write-back — only rebuild
 * the state slot containing that approval card (global modal / approval
 * center resolutions broadcast via pub/sub to all chat sessions). Idempotent:
 * duplicate notifications / no matching card return the original reference
 * without triggering a re-render. */
export function markApprovalResolvedInSessions(
  states: Record<string, ChatTurnState>,
  approvalId: string,
  status: ChatApprovalStatus,
): Record<string, ChatTurnState> {
  let changed = false;
  const next: Record<string, ChatTurnState> = {};
  for (const [sid, st] of Object.entries(states)) {
    const hasCard = st.messages.some((m) =>
      m.approvals.some((a) => a.approvalId === approvalId),
    );
    if (!hasCard) {
      next[sid] = st;
      continue;
    }
    changed = true;
    next[sid] = markApprovalResolved(st, approvalId, status);
  }
  return changed ? next : states;
}
