import i18n from "@/i18n";

/** 轻量 className 合并（不依赖上游 @/lib/utils cn）。 */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

/**
 * Vigil Console 共享逻辑——状态色调映射 + 筛选器 + YAML 预览 + 运行时长。
 * 纯函数，前端单测覆盖。
 */

export type StatusTone = "ok" | "warn" | "error" | "offline";

export const STATUS_OK = new Set(["running", "up", "healthy", "ok"]);
export const STATUS_WARN = new Set(["warning", "degraded", "unhealthy", "starting"]);
export const STATUS_ERROR = new Set(["error", "failed", "critical", "down", "crash", "failing"]);

/** Map a topology `status` field to a tone. Missing/unknown → offline (灰). */
export function statusTone(status?: string): StatusTone {
  const s = (status ?? "").trim().toLowerCase();
  if (!s || s === "unknown" || s === "stopped" || s === "offline") return "offline";
  if (STATUS_OK.has(s)) return "ok";
  if (STATUS_WARN.has(s)) return "warn";
  if (STATUS_ERROR.has(s)) return "error";
  return "offline";
}

export type StatusFilterId = "all" | StatusTone;

export interface StatusFilter {
  id: StatusFilterId;
  label: string;
}

/** 状态筛选器：全部 / 正常 / 告警 / 故障 / 离线。 */
export const STATUS_FILTERS: StatusFilter[] = [
  { id: "all", label: "全部" },
  { id: "ok", label: "正常" },
  { id: "warn", label: "告警" },
  { id: "error", label: "故障" },
  { id: "offline", label: "离线" },
];

export function statusMatchesFilter(status: string | undefined, filterId: StatusFilterId): boolean {
  if (filterId === "all") return true;
  return statusTone(status) === filterId;
}

/** 搜索匹配：name / type / env 子串（不区分大小写）。 */
export function matchesSearch(
  card: { name: string; type?: string; env?: string },
  q: string,
): boolean {
  if (!q) return true;
  const needle = q.trim().toLowerCase();
  if (!needle) return true;
  return `${card.name} ${card.type ?? ""} ${card.env ?? ""}`.toLowerCase().includes(needle);
}

/** Overview 指标聚合。 */
export function aggregateTopologyStats(
  view: { clusters: unknown[]; hosts: Array<{ services: unknown[] }> },
): { clusters: number; hosts: number; services: number } {
  return {
    clusters: view.clusters.length,
    hosts: view.hosts.length,
    services: view.hosts.reduce((n, h) => n + h.services.length, 0),
  };
}

/** 状态 pill 色类（CSS 变量在 index.css）。 */
export function statusPillClass(status?: string): string {
  switch (statusTone(status)) {
    case "ok":
      return "pill-ok";
    case "warn":
      return "pill-warn";
    case "error":
      return "pill-error";
    default:
      return "pill-offline";
  }
}

export function statusDotClass(status?: string): string {
  switch (statusTone(status)) {
    case "ok":
      return "dot-ok";
    case "warn":
      return "dot-warn";
    case "error":
      return "dot-error";
    default:
      return "dot-offline";
  }
}

/** task28 P0.3/P1.2：实体卡左侧 4px 状态条的颜色类（与 pill/dot 同一 token 源）。 */
export function statusAccentClass(status?: string): string {
  switch (statusTone(status)) {
    case "ok":
      return "border-l-[var(--vigil-ok)]";
    case "warn":
      return "border-l-[var(--vigil-warn)]";
    case "error":
      return "border-l-[var(--vigil-error)]";
    default:
      return "border-l-[var(--vigil-offline)]";
  }
}

export function envClass(env?: string): string {
  switch ((env ?? "").trim().toLowerCase()) {
    case "prod":
      return "env-prod";
    case "test":
      return "env-test";
    case "dev":
      return "env-dev";
    case "local":
      return "env-local";
    default:
      return "env-other";
  }
}

function yamlScalar(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "string") {
    return /^[\w.\-/@[\] \u4e00-\u9fff]+$/.test(v) && !/^[\s\-?]/.test(v)
      ? v
      : JSON.stringify(v);
  }
  return String(v);
}

/** Minimal YAML-flavoured serialization for the runbook preview (redacted data). */
export function yamlPreview(value: unknown, indent = 0): string {
  const pad = "  ".repeat(indent);
  if (Array.isArray(value)) {
    if (value.length === 0) return `${pad}[]`;
    return value
      .map((v) => {
        if (typeof v === "object" && v !== null && !Array.isArray(v)) {
          const entries = Object.entries(v as Record<string, unknown>);
          if (entries.length === 0) return `${pad}- {}`;
          const lines: string[] = [];
          entries.forEach(([k, val], i) => {
            const isObj = typeof val === "object" && val !== null;
            const keyPad = i === 0 ? `${pad}- ` : `${pad}  `;
            if (isObj) {
              lines.push(`${keyPad}${k}:`);
              lines.push(yamlPreview(val, indent + 2));
            } else {
              lines.push(`${keyPad}${k}: ${yamlScalar(val)}`);
            }
          });
          return lines.join("\n");
        }
        return `${pad}- ${yamlScalar(v)}`;
      })
      .join("\n");
  }
  if (typeof value === "object" && value !== null) {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return `${pad}{}`;
    return entries
      .map(([k, v]) => {
        if (typeof v === "object" && v !== null) {
          return `${pad}${k}:\n${yamlPreview(v, indent + 1)}`;
        }
        return `${pad}${k}: ${yamlScalar(v)}`;
      })
      .join("\n");
  }
  return `${pad}${yamlScalar(value)}`;
}

/** 运行时长格式化：秒 → "12h" / "3d 4h"。 */
export function formatUptime(seconds: number | undefined | null): string {
  if (seconds === undefined || seconds === null || !Number.isFinite(seconds) || seconds < 0) {
    return "-";
  }
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

// ── 批三十五：host 活性（lazy last_seen） ────────────────────────────────

/** 活性阈值：超过该分钟数无交互 → 视为"离线/无活动"（本批不做主动探测）。 */
export const LAST_SEEN_ACTIVE_MINUTES = 10;

export interface LastSeenInfo {
  label: string;
  tone: StatusTone;
}

/**
 * host 活性展示：last_seen 为 epoch 秒。
 * - 无记录 → "未探测"（offline）；
 * - 距现在 ≤ 阈值 → "在线 · X 分钟前活跃"（ok；<1 分钟 → "刚刚活跃"）；
 * - 超过阈值 → "离线 · 已 X 分钟无活动"（offline）。
 * label 经 i18n 输出（common.lastSeen.*），tone 保持原始语义。
 */
export function lastSeenInfo(lastSeen?: number, now: number = Date.now()): LastSeenInfo {
  if (!lastSeen || lastSeen <= 0) return { label: i18n.t("common.lastSeen.never"), tone: "offline" };
  const ageMin = Math.floor((now - lastSeen * 1000) / 60_000);
  if (ageMin < 0) return { label: i18n.t("common.lastSeen.justNow"), tone: "ok" };
  if (ageMin === 0) return { label: i18n.t("common.lastSeen.justNow"), tone: "ok" };
  if (ageMin <= LAST_SEEN_ACTIVE_MINUTES) {
    return { label: i18n.t("common.lastSeen.activeMin", { n: ageMin }), tone: "ok" };
  }
  return { label: i18n.t("common.lastSeen.inactiveMin", { n: ageMin }), tone: "offline" };
}

// ── Backend monitoring error markers ─────────────────────────────────────
// The dashboard backend emits these error strings in zh (default) or en
// (backend ops.lang=en), plus stable error codes; match any. Backend message
// i18n lives in hermes_cli/i18n.py (keys monitoring.prom_unavailable /
// monitoring.alertmanager_unavailable) — keep the markers in sync with both
// catalog languages.

/** Prometheus not-configured/unavailable detection (zh/en message text or error code). */
export function isPrometheusUnavailableError(text: string): boolean {
  return (
    text.includes("未配置 Prometheus") ||
    text.includes("Prometheus not configured") ||
    text.includes("prometheus_unavailable")
  );
}

/** Alertmanager not-configured detection (zh/en message text). */
export function isAlertmanagerUnconfiguredError(text: string): boolean {
  return text.includes("未配置 Alertmanager") || text.includes("Alertmanager not configured");
}

