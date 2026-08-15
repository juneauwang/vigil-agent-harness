/**
 * Vigil Console —— 精简数据层（复用后端 /api/*，凭据过滤不变）。
 *
 * 不复用 Hermes 前端的 api 客户端：只保留本控制台 7 页需要的只读端点。
 * 端点全部是公开只读（PUBLIC_API_PATHS），session token 有则带上（无害）。
 */

declare global {
  interface Window {
    __HERMES_BASE_PATH__?: string;
    __HERMES_SESSION_TOKEN__?: string;
  }
}

function readBasePath(): string {
  if (typeof window === "undefined") return "";
  const raw = window.__HERMES_BASE_PATH__ ?? "";
  if (!raw) return "";
  const withLead = raw.startsWith("/") ? raw : `/${raw}`;
  return withLead.replace(/\/+$/, "");
}

const BASE = readBasePath();

async function fetchJSON<T>(url: string): Promise<T> {
  const headers = new Headers();
  const token = typeof window !== "undefined" ? window.__HERMES_SESSION_TOKEN__ : undefined;
  if (token) headers.set("X-Hermes-Session-Token", token);
  const res = await fetch(`${BASE}${url}`, { headers, credentials: "include" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as T;
}

// ── Types (mirror the sanitized server payloads) ──────────────────────────

export interface TopologyCard {
  name: string;
  type?: string;
  env?: string;
  cluster?: string;
  endpoint?: string;
  role?: string;
  runtime?: string;
  status?: string;
  owner?: string;
  description?: string;
  port?: string | number;
  on_key_path?: boolean;
  kind?: string;
}

export interface TopologyService {
  card: TopologyCard;
  detail?: Record<string, unknown> | null;
}

export interface TopologyHost {
  card: TopologyCard;
  services: TopologyService[];
  services_missing?: boolean;
  detail?: Record<string, unknown> | null;
}

export interface TopologyCluster {
  name: string;
  env?: string;
  description?: string;
  owner?: string;
}

export interface TopologyView {
  generated_at: string;
  data_root: string;
  version?: number | string;
  sources?: string[];
  environments?: string[];
  clusters: TopologyCluster[];
  hosts: TopologyHost[];
  cross_host: TopologyService[];
  key_paths: string[][];
  key_path_entity_names: string[];
  details: Record<string, Record<string, unknown>>;
}

export interface TopologyResponse {
  ok: boolean;
  error?: string;
  data?: TopologyView;
}

export interface RunbookSummary {
  name: string;
  title: string;
  summary: string;
  triggers: string[];
  env?: string;
  kind?: string;
  checklist?: boolean;
  version?: number | string;
  step_count: number;
  updated_at?: string | null;
}

export interface RunbookListResponse {
  ok: boolean;
  error?: string;
  data?: { count: number; runbooks: RunbookSummary[] };
}

export interface RunbookDetailResponse {
  ok: boolean;
  error?: string;
  data?: Record<string, unknown>;
}

export interface HealthResponse {
  ok: boolean;
  version: string;
  auth_required: boolean;
  uptime_seconds?: number;
}

export interface StatusResponse {
  version?: string;
  release_date?: string;
  active_sessions?: number;
  gateway_running?: boolean;
  gateway_state?: string | null;
  config_version?: number;
  latest_config_version?: number;
  auth_required?: boolean;
}

export const api = {
  getTopology: () => fetchJSON<TopologyResponse>("/api/topology"),
  getRunbooks: () => fetchJSON<RunbookListResponse>("/api/runbooks"),
  getRunbook: (name: string) =>
    fetchJSON<RunbookDetailResponse>(`/api/runbooks/${encodeURIComponent(name)}`),
  getHealth: () => fetchJSON<HealthResponse>("/api/health"),
  getStatus: () => fetchJSON<StatusResponse>("/api/status"),
};
