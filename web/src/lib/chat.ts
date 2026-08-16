/**
 * 对话页纯逻辑（批三十一）——SSE 事件 → 消息列表的状态机。
 *
 * 从组件里抽成纯函数以便 node 环境单测（无 jsdom/testing-library）：
 *   - applyChatEvent()：chat:delta/tool/tool_result/approval_pending/done/error
 *     增量归并到消息列表
 *   - chatInputDisabled()：agent 忙时禁用输入（busy 语义照后端 409）
 *   - markApprovalResolved()：审批卡本地状态（批准/拒绝后回填）
 * 凭据红线：工具 input/output 摘要只显示服务端 redact 后的内容，前端不再加工。
 */

export interface ChatToolEvent {
  id: number;
  name: string;
  inputSummary: string;
  outputSummary?: string;
  ok?: boolean;
  expanded: boolean;
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

export interface ChatMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  streaming?: boolean;
  tools: ChatToolEvent[];
  approvals: ChatApprovalCard[];
  error?: string;
}

export interface ChatTurnState {
  messages: ChatMessage[];
  busy: boolean;
  nextId: number;
  activeMessageId: number | null;
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

export interface ChatEvent {
  type: string;
  data: Record<string, unknown>;
}

export function applyChatEvent(state: ChatTurnState, ev: ChatEvent): ChatTurnState {
  const data = ev.data ?? {};
  const next: ChatTurnState = {
    ...state,
    messages: state.messages.map((m) => ({ ...m, tools: [...m.tools], approvals: [...m.approvals] })),
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
        streaming: true,
        tools: [],
        approvals: [],
      });
    }
    return next;
  }

  if (ev.type === "chat:tool") {
    const active = activeAssistant(next);
    const name = String(data.name ?? "");
    const summary = String(data.input_summary ?? "");
    if (active) {
      active.tools.push({ id: next.nextId++, name, inputSummary: summary, expanded: false });
    } else {
      next.activeMessageId = next.nextId;
      next.nextId += 1;
      next.messages.push({
        id: next.activeMessageId,
        role: "assistant",
        content: "",
        streaming: true,
        tools: [{ id: next.nextId++, name, inputSummary: summary, expanded: false }],
        approvals: [],
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
      const tool = lastToolEvent(active, name);
      if (tool) {
        tool.outputSummary = summary;
        tool.ok = ok;
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
    } else {
      next.activeMessageId = next.nextId;
      next.nextId += 1;
      next.messages.push({
        id: next.activeMessageId,
        role: "assistant",
        content: "",
        streaming: true,
        tools: [],
        approvals: [card],
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
        tools: [],
        approvals: [],
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
      { id: state.nextId, role: "user", content: text, tools: [], approvals: [] },
    ],
  };
}

export function chatInputDisabled(state: ChatTurnState): boolean {
  return state.busy;
}

export function markApprovalResolved(
  state: ChatTurnState,
  approvalId: string,
  status: ChatApprovalStatus,
): ChatTurnState {
  return {
    ...state,
    messages: state.messages.map((m) => ({
      ...m,
      approvals: m.approvals.map((a) => (a.approvalId === approvalId ? { ...a, status } : a)),
    })),
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
