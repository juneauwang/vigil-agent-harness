/**
 * 审批裁决广播（批四十二 §BH）：全局审批弹窗 / 审批中心批准·拒绝成功后，把
 * 裁决结果广播给所有订阅方（对话页审批卡按 approvalId 回写状态）。
 *
 * 与 approvalPoller 的"只报 pending"互补：poller 无法区分批准 vs 拒绝，
 * 这里由发起方带明确终态广播；对话页 / 其它会话槽位按 id 幂等回写。
 */

export type ApprovalResolution = "approved" | "denied";

type ResolvedListener = (id: string, status: ApprovalResolution) => void;

const listeners = new Set<ResolvedListener>();

/** 裁决成功后的广播（批准/拒绝；由发起方调用一次）。 */
export function notifyApprovalResolved(id: string, status: ApprovalResolution): void {
  const fn = (listener: ResolvedListener) => {
    try {
      listener(id, status);
    } catch {
      // 订阅方异常不阻断其它广播。
    }
  };
  for (const listener of [...listeners]) fn(listener);
}

/** 订阅裁决广播；返回退订函数。 */
export function subscribeApprovalResolved(fn: ResolvedListener): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}
