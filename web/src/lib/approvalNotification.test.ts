// @vitest-environment jsdom
import { describe, expect, it, vi, afterEach } from "vitest";

import type { ApprovalItem } from "./api";
import {
  approvalNotificationBody,
  notificationPermission,
  requestApprovalNotificationPermission,
  resetApprovalNotificationState,
  showApprovalNotification,
} from "./approvalNotification";

const ITEM: ApprovalItem = {
  id: "apv_n1",
  command: "kubectl -n prod rollout restart deploy/gateway --timeout=300s",
  status: "pending",
  created_at: "2026-08-20T00:00:00Z",
  timeout_at: "2026-08-20T00:05:00Z",
};

class FakeNotification {
  static permission: NotificationPermission = "default";
  static requestPermission = vi.fn(async () => "granted");
  static instances: FakeNotification[] = [];
  title: string;
  options: NotificationOptions;
  onclick: (() => void) | null = null;
  close = vi.fn();
  constructor(title: string, options: NotificationOptions) {
    this.title = title;
    this.options = options;
    FakeNotification.instances.push(this);
  }
}

function installNotification() {
  FakeNotification.instances = [];
  (globalThis as Record<string, unknown>).Notification = FakeNotification;
}

afterEach(() => {
  vi.restoreAllMocks();
  FakeNotification.requestPermission.mockClear();
  FakeNotification.instances = [];
  delete (globalThis as Record<string, unknown>).Notification;
  resetApprovalNotificationState();
});

describe("审批桌面通知（批四十九）", () => {
  it("notificationPermission：无 Notification 支持 → denied（降级页内）", () => {
    expect(notificationPermission()).toBe("denied");
  });

  it("approvalNotificationBody：命令摘要 + 剩余时限 / 截断 / 无时限 / 已过期", () => {
    const now = Date.parse("2026-08-20T00:04:00Z");
    const body = approvalNotificationBody(ITEM, now);
    expect(body).toContain("kubectl -n prod rollout restart deploy/gateway --timeout=300s");
    expect(body).toContain("剩余 1m 0s");
    // 超长命令截断到 80 字符 + …
    const long = approvalNotificationBody({ ...ITEM, command: "x".repeat(120) }, now);
    expect(long.length).toBeLessThan(120);
    expect(long).toContain("…");
    // 无 timeout_at → 无剩余时间
    expect(approvalNotificationBody({ ...ITEM, timeout_at: null }, now)).not.toContain("剩余");
    // 已过 deadline → "即将超时"
    expect(approvalNotificationBody(ITEM, Date.parse("2026-08-20T00:06:00Z"))).toContain("即将超时");
  });

  it("showApprovalNotification：权限授予 → 发系统通知，点击聚焦窗口并关闭", () => {
    installNotification();
    FakeNotification.permission = "granted";
    const focusSpy = vi.spyOn(window, "focus").mockImplementation(() => {});
  const ok = showApprovalNotification(ITEM);
  expect(ok).toBe(true);
  const last = FakeNotification.instances.at(-1)!;
  expect(last.title).toBe("Vigil 需要审批");
  expect(last.options.body).toContain("kubectl -n prod rollout restart");
  expect(last.options.tag).toBe("apv_n1");
    last.onclick?.();
    expect(focusSpy).toHaveBeenCalled();
    expect(last.close).toHaveBeenCalled();
  });

  it("showApprovalNotification：权限 denied/未支持 → 返回 false（页内弹窗兜底）", () => {
    expect(showApprovalNotification(ITEM)).toBe(false);
    installNotification();
    FakeNotification.permission = "denied";
    expect(showApprovalNotification(ITEM)).toBe(false);
    expect(FakeNotification.instances).toHaveLength(0);
  });

  it("requestApprovalNotificationPermission：首次请求；拒绝后不再弹系统询问", async () => {
    installNotification();
    FakeNotification.permission = "default";
    FakeNotification.requestPermission.mockResolvedValueOnce("denied");
    expect(await requestApprovalNotificationPermission()).toBe(false);
    expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1);
    // 第二次：已请求过且被拒 → 直接 false，不再请求
    expect(await requestApprovalNotificationPermission()).toBe(false);
    expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1);
    // 重置后重新请求
    resetApprovalNotificationState();
    FakeNotification.permission = "default";
    FakeNotification.requestPermission.mockResolvedValueOnce("granted");
    expect(await requestApprovalNotificationPermission()).toBe(true);
  });

  it("requestApprovalNotificationPermission：已授予 → 直接 true 不重复请求", async () => {
    installNotification();
    FakeNotification.permission = "granted";
    expect(await requestApprovalNotificationPermission()).toBe(true);
    expect(FakeNotification.requestPermission).not.toHaveBeenCalled();
  });
});
