/**
 * Ops console shared logic — status tone mapping + filters (方向 1).
 *
 * Pure functions so the frontend can unit-test the mapping without a
 * server. Tone colors live in the theme (success/warning/destructive) plus
 * a gray for offline/unknown.
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

/** 状态筛选器（拓扑页/概览页共用）：全部 / 正常 / 告警 / 故障 / 离线。 */
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

/** Overview stat aggregation from the topology view. */
export function aggregateTopologyStats(
  view: {
    clusters: unknown[];
    hosts: Array<{ services: unknown[] }>;
  },
): { clusters: number; hosts: number; services: number } {
  return {
    clusters: view.clusters.length,
    hosts: view.hosts.length,
    services: view.hosts.reduce((n, h) => n + h.services.length, 0),
  };
}

/** Status pills for the high-density table / stat cards. */
export function statusPillClass(status?: string): string {
  switch (statusTone(status)) {
    case "ok":
      return "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400";
    case "warn":
      return "bg-amber-500/15 text-amber-600 dark:text-amber-400";
    case "error":
      return "bg-red-500/15 text-red-600 dark:text-red-400";
    default:
      return "bg-slate-500/15 text-slate-500 dark:text-slate-400";
  }
}

function yamlScalar(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "string") {
    // Plain-scalar-safe characters only (no `: ` / `#` / quotes); anything
    // else (e.g. "a: b") gets JSON-quoted so the preview stays valid YAML.
    return /^[\w.\-/@[\] \u4e00-\u9fff]+$/.test(v) && !/^[\s\-?]/.test(v)
      ? v
      : JSON.stringify(v);
  }
  return String(v);
}

/**
 * Minimal YAML-flavoured serialization for the runbook detail preview.
 *
 * The server already credential-redacts the payload (paths / keys / URL
 * userinfo masked) — this only re-serializes the sanitized dict so users
 * can read the runbook "as YAML" without a raw-file endpoint. Not a full
 * YAML emitter: enough for the v0.1 runbook shape (scalars + lists).
 */
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
              lines.push(yamlPreview(val, indent + (i === 0 ? 1 : 2)));
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

/** 运行时长格式化：秒 → "12h" / "3d 4h"（顶部栏徽标用）。 */
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
