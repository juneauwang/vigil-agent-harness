/**
 * 对话页纯逻辑（批三十一）——SSE 事件 → 消息列表的状态机。
 *
 * 从组件里抽成纯函数以便 node 环境单测（无 jsdom/testing-library）：
 *   - applyChatEvent()：chat:delta/chat:reasoning/tool/tool_result/
 *     approval_pending/done/error 增量归并到消息列表
 *   - chatInputDisabled()：agent 忙时禁用输入（busy 语义照后端 409）
 *   - markApprovalResolved()：审批卡本地状态（批准/拒绝后回填）
 *   - stateFromHistory()：批三十三历史端点 → 会话状态（切回恢复现场）
 * 批四十一：工具输出按服务端唯一 tool_id 挂接（并行同名单工具零错位）；
 * assistant 消息内工具/审批按事件到达顺序串成有序步骤序列。
 * 凭据红线：工具 input/output 摘要只显示服务端 redact 后的内容，前端不再加工。
 */

import type { ChatHistoryMessage } from "./api";

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

/** 有序步骤：工具调用与审批卡按事件到达顺序串成①②③…；ref 指向 tools/
 * approvals 数组里的条目。 */
export interface ChatStep {
  kind: ChatStepKind;
  /** tool 步骤 = ChatToolEvent.id；approval 步骤 = ChatApprovalCard.approvalId。 */
  ref: number | string;
  status: ChatStepStatus;
}

export interface ChatToolEvent {
  /** 本地自增 id（渲染 key + 步骤引用）。 */
  id: number;
  /** 服务端唯一 id（模型 tool_call id；空串 = 旧服务端/兜底按名挂接）。 */
  toolId: string;
  name: string;
  inputSummary: string;
  outputSummary?: string;
  ok?: boolean;
  expanded: boolean;
  /** 批四十二 §BJ：该工具调用导向的推理（chat:reasoning.tool_id 归属累加；
   * 历史结构化 reasoning.steps 按 tool_id 回填）。空串 = 无归属。 */
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

/** 批四十九：对话流内 clarify 卡片（问题 + choices + 自由文本，非全局弹窗）。 */
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
  /** 批四十一：推理过程纯文本（chat:reasoning 增量归并；历史端点带回）。 */
  reasoning: string;
  streaming?: boolean;
  tools: ChatToolEvent[];
  approvals: ChatApprovalCard[];
  /** 批四十九：clarify 卡片（对话流内呈现，与审批卡并列）。 */
  clarifies: ChatClarifyCard[];
  /** 批四十一：工具/审批按到达顺序的有序步骤序列。 */
  steps: ChatStep[];
  error?: string;
  /** 批三十六：用户点"停止"后的本地状态行（不持久化，切会话/重拉历史即消失）。 */
  interrupted?: boolean;
}

export interface ChatTurnState {
  messages: ChatMessage[];
  busy: boolean;
  nextId: number;
  activeMessageId: number | null;
}

/** 批三十八 §AW：审批卡是否已超时（timeout_at 过期且仍 pending）。 */
export function approvalIsTimedOut(
  card: Pick<ChatApprovalCard, "status" | "timeoutAt">,
  now: number = Date.now(),
): boolean {
  if (card.status !== "pending" || !card.timeoutAt) return false;
  const deadline = Date.parse(card.timeoutAt);
  if (Number.isNaN(deadline)) return false;
  return now >= deadline;
}

/** 批四十九：clarify 卡是否已超时（timeout_at 过期且仍 pending）。 */
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
  return { messages: [], busy: false, nextId: 1, activeMessageId: null };
}

/** 当前正在接收增量的事件（chat:delta 归并目标）。 */
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

/** 把 message 的 tools/approvals 换成新对象（applyChatEvent 各分支在副本上
 * 修改，避免污染旧 state 引用的嵌套字段）。 */
function cloneMessage(msg: ChatMessage): ChatMessage {
  return {
    ...msg,
    tools: msg.tools.map((t) => ({ ...t })),
    approvals: msg.approvals.map((a) => ({ ...a, timeoutAt: a.timeoutAt ?? null })),
    clarifies: msg.clarifies.map((c) => ({ ...c, timeoutAt: c.timeoutAt ?? null })),
    steps: msg.steps.map((s) => ({ ...s })),
  };
}

/** 只读查找步骤并返回新步骤数组（找不到返回原数组）。 */
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

  // 批四十一 §3：推理增量归并进 active 消息的 reasoning 字段（无 active
  // 时先创建消息——推理通常先于正文到达）。
  // 批四十二 §BJ：带 tool_id 的推理归属对应工具行（该步推理）；找不到归属
  // 工具时退回消息级（旧服务端/推理先于 chat:tool 到达的边界场景）。
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
      // 批四十一 §5：优先按服务端唯一 tool_id 挂接；旧服务端无 id 时兜底
      // 按"最后一个同名未出结果"挂接（保持旧行为）。
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

  // 批四十九：LLM 调 clarify → 对话流内挂问题卡片（非全局弹窗；审批卡并列）。
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
      // 回合已结束：仍挂 pending 的 clarify 卡收口（agent 已带着应答/超时
      // 继续）——状态交由卡片自身倒计时/应答回写精确展示。
      for (const c of active.clarifies) {
        if (c.status === "pending") c.status = clarifyIsTimedOut(c) ? "timed_out" : "answered";
      }
    }
    next.busy = false;
    next.activeMessageId = null;
    return next;
  }

  if (ev.type === "chat:error") {
    const msg = String(data.message ?? "对话出错");
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

  return next;
}

/** 用户消息入列（发送时立即渲染气泡）。 */
export function pushUserMessage(state: ChatTurnState, text: string): ChatTurnState {
  return {
    ...state,
    busy: true,
    activeMessageId: null,
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

/** 批三十六：用户点"停止"后的本地状态收口——busy 解除 + 消息区"已停止"状态行。

 * 服务端 turn 由 /interrupt 真中断（后台仍在收尾，busy 由注册表轮询兜底复位）；
 * 这里只做本地即时反馈：可立即发新消息（后端 409 会在极端竞态下兜底）。
 */
export function markTurnInterrupted(state: ChatTurnState): ChatTurnState {
  const last = state.messages[state.messages.length - 1];
  // 幂等：末条已是本地"已停止"行（轮询恢复历史后重挂）不重复追加。
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

/** 批四十九：clarify 卡本地收口（提交成功 → answered；提交失败 → error；
 * 倒计时到期 → timed_out）。步骤状态同步。 */
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

/** 历史消息 → 会话状态（切回/切页恢复现场；批三十三）。
 *
 * 服务器消息带稳定行 id，这里重编号为本地自增（避免与流式渲染的 nextId
 * 冲突）；tools 已由服务端折叠进 assistant 气泡（含 tool_id/reasoning）。
 * 批四十二 §BJ：历史 reasoning 兼容单值字符串（消息级）与结构化
 * {steps:[{tool_id,text}]}（按 tool_id 挂回对应工具行的"该步推理"）。
 * busy 来自注册表（在跑的会话显示"处理中"，输入禁用直到后台 turn 完成）。
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
    return {
      id,
      role: "assistant",
      content: m.content ?? "",
      reasoning: typeof rawReasoning === "string" ? rawReasoning : "",
      streaming: false,
      tools,
      approvals: [],
      clarifies: [],
      steps: tools.map((t) => ({
        kind: "tool" as const,
        ref: t.id,
        status: t.ok === false ? ("failed" as const) : ("done" as const),
      })),
    };
  });
  return { messages, busy: Boolean(busy), nextId, activeMessageId: null };
}

/** 批四十二 §BH：跨会话审批裁决回写——只重建含该审批卡的状态槽（全局弹窗/
 * 审批中心裁决后经 pub/sub 广播到所有 chat 会话）。幂等：重复通知/无匹配卡
 * 返回原引用不触发重渲染。 */
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
