import { afterEach, describe, expect, it, vi } from "vitest";

import { ApprovalPoller } from "./approvalPoller";
import type { ApprovalItem, ApprovalsResponse } from "./api";

const item = (id: string, over: Partial<ApprovalItem> = {}): ApprovalItem => ({
  id,
  command: `cmd-${id}`,
  description: "该命令需要审批",
  env: "dev",
  grade: "3",
  source: "web",
  status: "pending",
  created_at: "2026-08-16T00:00:00Z",
  timeout_at: null,
  ...over,
});

const resp = (items: ApprovalItem[], total?: number): ApprovalsResponse => ({
  approvals: items,
  total: total ?? items.length,
});

describe("审批全局轮询（批三十四）", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("新 id 只上报一次（已见集合去重）", async () => {
    const fetcher = vi.fn().mockResolvedValue(resp([item("a"), item("b")]));
    const p = new ApprovalPoller(fetcher);
    const snaps: string[][] = [];
    p.subscribe((s) => snaps.push(s.added.map((a) => a.id)));

    await p.poll();
    expect(snaps[0]).toEqual(["a", "b"]);
    await p.poll();
    expect(snaps[1]).toEqual([]);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("id 从 pending 消失（批准/拒绝/超时）→ 从已见集合释放", async () => {
    const fetcher = vi.fn();
    const p = new ApprovalPoller(fetcher);
    fetcher.mockResolvedValueOnce(resp([item("a")]));
    fetcher.mockResolvedValueOnce(resp([]));
    fetcher.mockResolvedValueOnce(resp([item("a")]));

    const addedIds: string[][] = [];
    p.subscribe((s) => addedIds.push(s.added.map((a) => a.id)));
    await p.poll();
    await p.poll();
    await p.poll();
    expect(addedIds).toEqual([["a"], [], ["a"]]);
  });

  it("已见集合上限保护（超出只上报前 200，且不会反复重报）", async () => {
    const many = Array.from({ length: 205 }, (_, i) => item(`id-${i}`));
    const fetcher = vi.fn().mockResolvedValue(resp(many));
    const p = new ApprovalPoller(fetcher);
    const addedCounts: number[] = [];
    p.subscribe((snap) => addedCounts.push(snap.added.length));

    await p.poll();
    await p.poll();
    expect(addedCounts).toEqual([200, 0]);
  });

  it("start 幂等 + 周期轮询 + stop 停止", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(resp([]));
    const p = new ApprovalPoller(fetcher, 4000);
    p.start();
    p.start(); // 幂等：不额外启动一个定时器
    await vi.advanceTimersByTimeAsync(4000 * 3);
    expect(fetcher).toHaveBeenCalledTimes(4); // 启动立即 1 次 + 3 次周期
    p.stop();
    await vi.advanceTimersByTimeAsync(4000 * 2);
    expect(fetcher).toHaveBeenCalledTimes(4);
  });

  it("默认轮询间隔 ≤3s（§BT：审批产生后弹窗及时出现）", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(resp([]));
    const p = new ApprovalPoller(fetcher); // 用默认间隔（3000ms）
    p.start();
    expect(fetcher).toHaveBeenCalledTimes(1); // 启动立即拉一次
    await vi.advanceTimersByTimeAsync(3000);
    expect(fetcher).toHaveBeenCalledTimes(2); // 3s 内到点
    p.stop();
  });

  it("重新 start 前 stop 停止后再 start 可恢复轮询（页面挂载/卸载语义）", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(resp([]));
    const p = new ApprovalPoller(fetcher, 3000);
    p.start();
    p.stop(); // 模拟路由卸载（App 根组件 unmount）
    await vi.advanceTimersByTimeAsync(3000 * 3);
    expect(fetcher).toHaveBeenCalledTimes(1); // 停止后不再轮询
    p.start(); // 模拟重新挂载：恢复轮询
    await vi.advanceTimersByTimeAsync(3000);
    expect(fetcher).toHaveBeenCalledTimes(3); // 恢复：立即 1 次 + 周期 1 次
    p.stop();
  });

  it("轮询失败保留上次快照，不崩溃，下个 tick 恢复", async () => {
    const fetcher = vi.fn()
      .mockRejectedValueOnce(new Error("network down"))
      .mockResolvedValueOnce(resp([item("a")], 1));
    const p = new ApprovalPoller(fetcher);

    await p.poll(); // 失败：不抛、快照保持空
    expect(p.getSnapshot().total).toBe(0);
    expect(p.getSnapshot().approvals).toEqual([]);
    await p.poll(); // 恢复
    expect(p.getSnapshot().total).toBe(1);
    expect(p.getSnapshot().approvals.map((a) => a.id)).toEqual(["a"]);
  });

  it("快照携带 total，订阅可退订", async () => {
    const fetcher = vi.fn().mockResolvedValue(resp([item("a"), item("b")], 7));
    const p = new ApprovalPoller(fetcher);
    const seen: number[] = [];
    const unsub = p.subscribe((s) => seen.push(s.total));
    await p.poll();
    unsub();
    await p.poll();
    expect(seen).toEqual([7]);
  });
});
