import type { ApprovalItem } from "./api";

/**
 * 审批弹窗队列纯逻辑（批三十四）：多个 pending 审批排队、一次弹一个；
 * 当前处理完（批准/拒绝/忽略）弹下一个。与对话页内嵌审批卡互相独立——
 * 各自管各自的状态，不共用。
 */
export interface ApprovalQueueState {
  /** 待处理审批（queue[0] = 当前弹窗展示项）。 */
  queue: ApprovalItem[];
  /** 正在提交批准/拒绝（按钮禁用，防重复提交）。 */
  busy: boolean;
  /** 最近一次操作错误（提交失败展示；忽略后清空）。 */
  error: string | null;
}

export const initialApprovalQueue: ApprovalQueueState = {
  queue: [],
  busy: false,
  error: null,
};

/** 新审批入队（按 id 去重；已存在不动；保持到达顺序）。 */
export function enqueueApprovals(
  state: ApprovalQueueState,
  added: ApprovalItem[] | undefined,
): ApprovalQueueState {
  if (!added || added.length === 0) return state;
  const existing = new Set(state.queue.map((a) => a.id));
  const fresh = added.filter((a) => a && !existing.has(a.id));
  if (fresh.length === 0) return state;
  return { ...state, queue: [...state.queue, ...fresh] };
}

/** 移除（批准/拒绝/忽略成功后；清空错误让下一个正常展示）。 */
export function dequeueApproval(state: ApprovalQueueState, id: string): ApprovalQueueState {
  const queue = state.queue.filter((a) => a.id !== id);
  return { ...state, queue, error: null };
}

/** 当前审批已不在 pending（被别处裁决/超时）→ 自动移出队列。 */
export function dropResolvedApprovals(
  state: ApprovalQueueState,
  pendingIds: Set<string>,
): ApprovalQueueState {
  const queue = state.queue.filter((a) => pendingIds.has(a.id));
  if (queue.length === state.queue.length) return state;
  return { ...state, queue };
}

export function setQueueBusy(state: ApprovalQueueState, busy: boolean): ApprovalQueueState {
  return { ...state, busy };
}

export function setQueueError(state: ApprovalQueueState, error: string | null): ApprovalQueueState {
  return { ...state, error };
}
