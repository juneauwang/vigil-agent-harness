import { describe, expect, it } from "vitest";

import {
  dequeueApproval,
  dropResolvedApprovals,
  enqueueApprovals,
  initialApprovalQueue,
  setQueueBusy,
  setQueueError,
} from "./approvalQueue";
import type { ApprovalItem } from "./api";

const item = (id: string, over: Partial<ApprovalItem> = {}): ApprovalItem => ({
  id,
  command: `cmd-${id}`,
  description: "该命令需要审批",
  env: "dev",
  grade: "3",
  status: "pending",
  created_at: "2026-08-16T00:00:00Z",
  ...over,
});

describe("审批弹窗队列（批三十四）", () => {
  it("新审批入队保持到达顺序，按 id 去重", () => {
    let s = enqueueApprovals(initialApprovalQueue, [item("a"), item("b")]);
    expect(s.queue.map((x) => x.id)).toEqual(["a", "b"]);
    // 同一批里重复 id 去重
    s = enqueueApprovals(s, [item("b"), item("c"), item("b")]);
    expect(s.queue.map((x) => x.id)).toEqual(["a", "b", "c"]);
  });

  it("空新增/空队列不产生新引用", () => {
    expect(enqueueApprovals(initialApprovalQueue, undefined)).toBe(initialApprovalQueue);
    expect(enqueueApprovals(initialApprovalQueue, [])).toBe(initialApprovalQueue);
  });

  it("忽略语义 = dequeue：移除当前项并清空错误", () => {
    let s = enqueueApprovals(initialApprovalQueue, [item("a"), item("b")]);
    s = setQueueError(s, "boom");
    s = dequeueApproval(s, "a");
    expect(s.queue.map((x) => x.id)).toEqual(["b"]);
    expect(s.error).toBeNull();
  });

  it("批准/拒绝成功移除后弹下一个（queue[0] 轮转）", () => {
    let s = enqueueApprovals(initialApprovalQueue, [item("a"), item("b"), item("c")]);
    expect(s.queue[0].id).toBe("a");
    s = dequeueApproval(s, "a");
    expect(s.queue[0].id).toBe("b");
    s = dequeueApproval(s, "b");
    expect(s.queue[0].id).toBe("c");
  });

  it("当前审批被别处裁决/超时（不在 pending）→ 自动移出", () => {
    let s = enqueueApprovals(initialApprovalQueue, [item("a"), item("b")]);
    s = dropResolvedApprovals(s, new Set(["b"]));
    expect(s.queue.map((x) => x.id)).toEqual(["b"]);
  });

  it("busy/error 状态位（提交中禁用按钮）", () => {
    let s = setQueueBusy(initialApprovalQueue, true);
    expect(s.busy).toBe(true);
    s = setQueueError(s, "[timeout] 审批已超时");
    expect(s.error).toContain("超时");
  });
});
