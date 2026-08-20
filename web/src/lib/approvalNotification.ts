/**
 * 批四十九：审批桌面通知（Web Notification API）。
 *
 * 审批只在 Vigil 页面内可见，用户切到其他程序就错过 → 本模块把新审批以
 * 系统级通知送出（标题"Vigil 需要审批" + 命令摘要 + 剩余时限），点击通知 →
 * 聚焦/打开 dashboard（审批弹窗全局常驻，聚焦即见）。权限首次触发时请求；
 * 拒绝则降级为仅页内弹窗，不阻塞审批功能。页面打开期间生效（本地 dashboard
 * 常驻页面，无需 Service Worker）。
 *
 * 测试友好：纯函数 + 无副作用的权限门，Notification mock 可验证。
 */

import type { ApprovalItem } from "./api";

let permissionRequested = false;

/** 当前通知权限（无 Notification 支持 = denied，降级页内弹窗）。 */
export function notificationPermission(): NotificationPermission {
  if (typeof Notification === "undefined") return "denied";
  return Notification.permission;
}

/** 通知正文：命令摘要（前 80 字符，单行）+ 剩余时限（timeout_at 推导）。 */
export function approvalNotificationBody(item: ApprovalItem, now: number = Date.now()): string {
  const cmd = (item.command ?? "").replace(/\s+/g, " ").trim();
  const summary = cmd.length > 80 ? `${cmd.slice(0, 80)}…` : cmd;
  let time = "";
  if (item.timeout_at) {
    const ms = Date.parse(item.timeout_at) - now;
    if (!Number.isNaN(ms) && ms > 0) {
      const total = Math.ceil(ms / 1000);
      const m = Math.floor(total / 60);
      const s = total % 60;
      time = ` · 剩余 ${m > 0 ? `${m}m ${s}s` : `${s}s`}`;
    } else if (!Number.isNaN(ms)) {
      time = " · 即将超时";
    }
  }
  return `${summary}${time}`;
}

/** 发送一条审批通知（权限未授予 → 静默返回 false，页内弹窗兜底）。 */
export function showApprovalNotification(item: ApprovalItem): boolean {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") {
    return false;
  }
  try {
    const n = new Notification("Vigil 需要审批", {
      body: approvalNotificationBody(item),
      tag: item.id,
    });
    n.onclick = () => {
      try {
        window.focus();
      } catch {
        /* 聚焦失败不影响审批功能 */
      }
      try {
        n.close();
      } catch {
        /* ignore */
      }
    };
    return true;
  } catch {
    return false;
  }
}

/**
 * 首次触发时请求通知权限（幂等：已请求过不再弹系统询问）。
 * 拒绝 → 后续调用直接返回 false（降级页内弹窗，不打扰）。
 */
export async function requestApprovalNotificationPermission(): Promise<boolean> {
  if (typeof Notification === "undefined") return false;
  if (Notification.permission === "granted") return true;
  if (Notification.permission === "denied") return false;
  if (permissionRequested) return false;
  permissionRequested = true;
  try {
    const perm = await Notification.requestPermission();
    return perm === "granted";
  } catch {
    return false;
  }
}

/** 测试用：重置模块级"已请求"标记（每用例隔离）。 */
export function resetApprovalNotificationState(): void {
  permissionRequested = false;
}
