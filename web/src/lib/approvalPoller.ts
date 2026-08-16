import { useEffect, useState } from "react";
import { api, type ApprovalItem, type ApprovalsResponse } from "./api";

/**
 * 全局审批轮询（批三十四）：低频（分钟级）事件走前端轮询而非 SSE。
 *
 * 单例持有"已见 pending id"集合——新 id 经 snapshot.added 通知（弹窗入队），
 * 同一 id 不重复上报；id 从 pending 消失（批准/拒绝/超时）即从已见集合释放
 * （防无限膨胀，另加上限保护）。轮询失败保留上次快照，下个 tick 重试。
 */
export interface ApprovalSnapshot {
  approvals: ApprovalItem[];
  total: number;
  /** 本次轮询新出现的 pending 审批（弹窗据此入队）。 */
  added: ApprovalItem[];
}

const MAX_SEEN_IDS = 200;
const POLL_INTERVAL_MS = 4000;

export class ApprovalPoller {
  private seenIds = new Set<string>();
  private approvals: ApprovalItem[] = [];
  private total = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private inFlight: Promise<void> | null = null;
  private listeners = new Set<(snap: ApprovalSnapshot) => void>();
  private fetchPending: () => Promise<ApprovalsResponse>;
  private intervalMs: number;

  constructor(
    fetchPending: () => Promise<ApprovalsResponse> = () =>
      api.getApprovals({ status: "pending", limit: 200 }),
    intervalMs: number = POLL_INTERVAL_MS,
  ) {
    this.fetchPending = fetchPending;
    this.intervalMs = intervalMs;
  }

  /** 幂等启动：立即拉一次 + 周期轮询。 */
  start(): void {
    if (this.timer !== null) return;
    void this.poll();
    this.timer = setInterval(() => void this.poll(), this.intervalMs);
  }

  stop(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  /** 立即拉一次（批准/拒绝后手动刷新角标，不等下一个 tick）。 */
  refresh(): Promise<void> {
    return this.poll();
  }

  async poll(): Promise<void> {
    if (this.inFlight) return this.inFlight;
    this.inFlight = (async () => {
      let resp: ApprovalsResponse;
      try {
        resp = await this.fetchPending();
      } catch {
        // 网络抖动：保留上次快照，下个 tick 重试。
        return;
      }
      const pending = resp.approvals ?? [];
      const pendingIds = new Set(pending.map((a) => a.id));
      // 已离开 pending（批准/拒绝/超时）的 id 从已见集合释放。
      for (const id of [...this.seenIds]) {
        if (!pendingIds.has(id)) this.seenIds.delete(id);
      }
      // 新 id 上报并入已见（上限保护：超过上限不再接收，防无限膨胀）。
      const added: ApprovalItem[] = [];
      for (const a of pending) {
        if (this.seenIds.has(a.id)) continue;
        if (this.seenIds.size >= MAX_SEEN_IDS) break;
        this.seenIds.add(a.id);
        added.push(a);
      }
      this.approvals = pending;
      this.total = resp.total ?? pending.length;
      this.emit({ approvals: this.approvals, total: this.total, added });
    })();
    try {
      await this.inFlight;
    } finally {
      this.inFlight = null;
    }
  }

  getSnapshot(): ApprovalSnapshot {
    return { approvals: this.approvals, total: this.total, added: [] };
  }

  subscribe(fn: (snap: ApprovalSnapshot) => void): () => void {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  }

  private emit(snap: ApprovalSnapshot): void {
    for (const fn of [...this.listeners]) fn(snap);
  }
}

/** 应用级单例（App.tsx 启动轮询；弹窗/铃铛订阅快照）。 */
export const approvalPoller = new ApprovalPoller();

/** 只读订阅快照（弹窗/铃铛/顶栏角标用；不负责 start/stop）。 */
export function useApprovalSnapshot(): ApprovalSnapshot {
  const [snap, setSnap] = useState<ApprovalSnapshot>(() => approvalPoller.getSnapshot());
  useEffect(() => approvalPoller.subscribe(setSnap), []);
  return snap;
}

/** 启动轮询 + 订阅快照（App 根组件唯一持有者）。 */
export function useApprovalPolling(): ApprovalSnapshot {
  useEffect(() => {
    approvalPoller.start();
    return () => approvalPoller.stop();
  }, []);
  return useApprovalSnapshot();
}
