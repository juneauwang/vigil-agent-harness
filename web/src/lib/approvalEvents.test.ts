import { describe, expect, it, vi } from "vitest";
import { notifyApprovalResolved, subscribeApprovalResolved } from "./approvalEvents";

describe("批四十二 §BH 审批裁决广播", () => {
  it("订阅方收到 id + 终态；退订后不再收到", () => {
    const fn = vi.fn();
    const unsub = subscribeApprovalResolved(fn);
    notifyApprovalResolved("apv_1", "approved");
    expect(fn).toHaveBeenCalledWith("apv_1", "approved");
    unsub();
    notifyApprovalResolved("apv_1", "denied");
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("单个订阅方抛错不阻断其它广播", () => {
    const boom = vi.fn(() => {
      throw new Error("boom");
    });
    const ok = vi.fn();
    subscribeApprovalResolved(boom);
    subscribeApprovalResolved(ok);
    notifyApprovalResolved("apv_2", "denied");
    expect(ok).toHaveBeenCalledWith("apv_2", "denied");
  });
});
