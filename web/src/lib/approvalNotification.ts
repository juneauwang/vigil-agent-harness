/**
 * Batch 49: approval desktop notifications (Web Notification API).
 *
 * Approvals are only visible inside the Vigil page — the user misses them when
 * switching to another app → this module pushes new approvals as system-level
 * notifications (title "Vigil needs approval" + command summary + time left);
 * clicking the notification focuses/opens the dashboard (the approval popup is
 * globally resident, so focusing reveals it). Permission is requested on first
 * trigger; denial degrades to the in-page popup only, without blocking
 * approvals. Active while the page is open (the local dashboard keeps a
 * resident page, no Service Worker needed).
 *
 * Test-friendly: pure functions + a side-effect-free permission gate; the
 * Notification mock is verifiable.
 */

import type { ApprovalItem } from "./api";
import i18n from "@/i18n";

let permissionRequested = false;

/** Current notification permission (no Notification support = denied, in-page popup fallback). */
export function notificationPermission(): NotificationPermission {
  if (typeof Notification === "undefined") return "denied";
  return Notification.permission;
}

/** Notification body: command summary (first 80 chars, single line) + time left (derived from timeout_at). */
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
      time = i18n.t("lib.notifyRemaining", { time: m > 0 ? `${m}m ${s}s` : `${s}s` });
    } else if (!Number.isNaN(ms)) {
      time = i18n.t("lib.notifyExpiring");
    }
  }
  return `${summary}${time}`;
}

/** Send one approval notification (permission not granted → silently return false; in-page popup covers it). */
export function showApprovalNotification(item: ApprovalItem): boolean {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") {
    return false;
  }
  try {
    const n = new Notification(i18n.t("lib.notifyTitle"), {
      body: approvalNotificationBody(item),
      tag: item.id,
    });
    n.onclick = () => {
      try {
        window.focus();
      } catch {
        /* focus failure doesn't affect the approval flow */
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
 * Request notification permission on first trigger (idempotent: no repeat
 * system prompt once asked). Denial → subsequent calls return false directly
 * (degrades to the in-page popup, no nagging).
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

/** Test hook: reset the module-level "requested" flag (per-case isolation). */
export function resetApprovalNotificationState(): void {
  permissionRequested = false;
}
