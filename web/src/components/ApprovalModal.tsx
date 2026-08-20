import { useCallback, useEffect, useReducer, useRef, useState } from "react";
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
 * 审批全局弹窗（批三十四）：任何路由可见的页面内居中 modal。
 *
 * 队列语义：poller 上报的新审批入队，一次弹一个；批准/拒绝成功后移除并弹
 * 下一个；忽略 = 移除队列（poller 已标记 seen，本次会话不再弹，铃铛角标与
 * 审批中心保留，命令继续等待，超时策略照现有 timeout_policy）。当前审批被
 * 别处裁决/超时 → 自动移出队列。无 ESC/遮罩点击关闭——忽略是显式动作，避免
 * 误触把安全交互藏掉。
 *
 * 批四十九：形态改右下角卡片（Grafana 通知风格，可堆叠排队）；审批到达 →
 * 桌面通知（Web Notification API，权限首次请求，拒绝降级页内弹窗）；弹窗带
 * 超时倒计时（timeout_at 推导，快超时红色）。安全语义不变：无自动消失、批准/
 * 拒绝/忽略三按钮显式、队列一次一个、全局可见。
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

/** 批四十一 §6：长命令统一等宽代码块 + max-height 滚动 + 展开全文（保留缩进）。 */
function LongCommandBlock({ command }: { command: string }) {
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
          {expanded ? "收起" : "展开全文"}
        </button>
      )}
    </div>
  );
}

/** 批四十九：审批超时倒计时（timeout_at 推导；已过期 → "审批超时，已终止"）。 */
function ApprovalCountdown({ timeoutAt, pending }: { timeoutAt?: string | null; pending: boolean }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  if (!pending || !timeoutAt) return null;
  const deadline = Date.parse(timeoutAt);
  if (Number.isNaN(deadline)) return null;
  const ms = deadline - now;
  if (ms <= 0) return <span className="shrink-0 text-[10px] text-red-500">审批超时，已终止</span>;
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
  const snap = useApprovalSnapshot();
  const [state, dispatch] = useReducer(queueReducer, initialApprovalQueue);
  const active = state.queue[0] ?? null;
  // 批四十一 §2：提交中同步防重入（state.busy 只禁下一帧渲染的按钮，连点/重放
  // 会用 ref 挡住第二枪）。
  const inFlightRef = useRef(false);

  // poller 上报的新审批 → 入队。
  useEffect(() => {
    if (snap.added.length > 0) {
      dispatch({ type: "enqueue", added: snap.added });
      // 批四十九：审批到达 → 桌面通知（权限首次请求；拒绝降级页内弹窗）。
      void (async () => {
        const granted = await requestApprovalNotificationPermission();
        if (granted) {
          for (const item of snap.added) showApprovalNotification(item);
        }
      })();
    }
  }, [snap.added]);

  // 当前审批已不在 pending（别处批准/拒绝/超时）→ 自动移出队列。
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
        // 批四十二 §BH：广播裁决结果——对话页审批卡按 approvalId 同步回写，
        // 弹窗关闭全局生效，不再等轮询/重拉历史。
        notifyApprovalResolved(item.id, action);
        // 手动刷新一次，角标/审批中心不等下一个轮询 tick。
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
      aria-label="审批请求"
    >
      <div className="flex flex-col gap-3 rounded-lg border border-[var(--vigil-border)] bg-[var(--vigil-card)] p-4 shadow-[0_8px_30px_rgba(0,0,0,0.2)]">
        <div className="flex items-center gap-2">
          <ShieldAlert className="size-5 shrink-0 text-amber-500" />
          <span className="text-sm font-semibold text-[var(--vigil-text)]">该命令需要审批</span>
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
            <EyeOff className="size-3.5" /> 忽略
          </button>
          <button
            type="button"
            disabled={state.busy}
            onClick={() => void resolve(active, "denied")}
            className="vigil-btn h-8 whitespace-nowrap border border-red-500/50 px-2.5 text-xs text-red-600 dark:text-red-400 disabled:opacity-50"
          >
            <X className="size-3.5" /> 拒绝
          </button>
          <button
            type="button"
            disabled={state.busy}
            onClick={() => void resolve(active, "approved")}
            className="vigil-btn h-8 whitespace-nowrap border border-emerald-500/50 px-2.5 text-xs text-emerald-600 dark:text-emerald-400 disabled:opacity-50"
          >
            <Check className="size-3.5" /> 批准
          </button>
        </div>
      </div>
      {state.queue.length > 1 && (
        <div className="rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-3 py-1.5 text-[10px] text-[var(--vigil-muted)]">
          还有 {state.queue.length - 1} 个审批待处理
        </div>
      )}
    </div>
  );
}
