/**
 * Vigil Console —— 精简数据层（复用后端 /api/*，凭据过滤不变）。
 *
 * 本批按「UI 壳批二十八 API 契约」（2026-08-15）接入 Approvals / Audit /
 * Exec / Incidents / Sessions。后端由 Codex 实现，未就绪时端点 404 →
 * 页面按错误信封显示空态；开发期可用 mock 模式（localStorage vigil-mock=1
 * 或 ?mock=1）加载文档 IP 占位数据（lib/mock.ts）。
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

/** 契约错误信封：`{"error": {"code", "message", "details"}}`。 */
export class ApiError extends Error {
  code: string;
  details?: Record<string, unknown>;
  status: number;

  constructor(code: string, message: string, status: number, details?: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

function parseErrorBody(text: string, status: number): ApiError {
  try {
    const body = JSON.parse(text) as {
      error?: { code?: string; message?: string; details?: Record<string, unknown> };
      detail?: string;
    };
    if (body.error && typeof body.error === "object") {
      return new ApiError(
        body.error.code ?? "internal",
        body.error.message ?? body.detail ?? `HTTP ${status}`,
        status,
        body.error.details,
      );
    }
    return new ApiError("internal", body.detail ?? `HTTP ${status}`, status);
  } catch {
    return new ApiError("internal", `HTTP ${status}`, status);
  }
}

async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  const token = typeof window !== "undefined" ? window.__HERMES_SESSION_TOKEN__ : undefined;
  if (token && !headers.has("X-Hermes-Session-Token")) {
    headers.set("X-Hermes-Session-Token", token);
  }
  if (init?.body && typeof init.body === "string") {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${BASE}${url}`, { ...init, headers, credentials: "include" });
  const text = await res.text();
  if (!res.ok) {
    throw parseErrorBody(text, res.status);
  }
  return (text ? JSON.parse(text) : {}) as T;
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

// ── 批二十八契约类型 ────────────────────────────────────────────────────────

export type ApprovalStatus = "pending" | "approved" | "denied" | "timeout";
export type ApprovalScope = "once" | "session" | "permanent";

export interface ApprovalItem {
  id: string;
  command: string;
  description?: string;
  env?: string;
  grade?: string;
  session_key?: string;
  source?: "cli" | "gateway" | "web";
  status: ApprovalStatus;
  scope?: ApprovalScope | null;
  created_at: string;
  timeout_at?: string | null;
  allow_session?: boolean;
  allow_permanent?: boolean;
}

export interface ApprovalsResponse {
  approvals?: ApprovalItem[];
  total?: number;
  has_more?: boolean;
  error?: { code?: string; message?: string; details?: Record<string, unknown> };
}

export interface AuditSessionSummary {
  session_id: string;
  event_count: number;
  started_at?: string | null;
  ended_at?: string | null;
}

export interface AuditSessionsResponse {
  sessions?: AuditSessionSummary[];
  total?: number;
  has_more?: boolean;
  error?: { code?: string; message?: string; details?: Record<string, unknown> };
}

export interface AuditEvent {
  seq: number;
  ts?: string;
  type?: string;
  command?: string;
  result?: { exit_code?: number; output_preview?: string } | null;
  [key: string]: unknown;
}

export interface AuditEventsResponse {
  events?: AuditEvent[];
  total?: number;
  has_more?: boolean;
  error?: { code?: string; message?: string; details?: Record<string, unknown> };
}

export type ExecStatus = "executed" | "needs_approval" | "denied" | "pending" | "error";

export interface ExecResponse {
  status: ExecStatus;
  exec_id?: string;
  approval_id?: string;
  pending?: boolean;
  exit_code?: number;
  output?: string;
  code?: string;
  reason?: string;
  error?: { code?: string; message?: string; details?: Record<string, unknown> };
}

export interface ExecRecord {
  exec_id: string;
  command?: string;
  env?: string;
  host?: string;
  status?: ExecStatus;
  exit_code?: number;
  output?: string;
  executed_at?: string;
  duration_ms?: number;
  approval_id?: string;
  error?: { code?: string; message?: string; details?: Record<string, unknown> };
}

export interface IncidentsResponse {
  incidents?: unknown[];
  total?: number;
  schema_version?: number;
  error?: { code?: string; message?: string; details?: Record<string, unknown> };
}

export interface SessionSummary {
  session_id: string;
  title?: string | null;
  started_at?: string;
  last_activity_at?: string | null;
  model?: string | null;
  running?: boolean;
}

export interface SessionsResponse {
  sessions?: SessionSummary[];
  total?: number;
  has_more?: boolean;
  error?: { code?: string; message?: string; details?: Record<string, unknown> };
}

// ── API methods ────────────────────────────────────────────────────────────

export const api = {
  // 第一批：拓扑 / runbook / 状态 / 健康
  getTopology: () => fetchJSON<TopologyResponse>("/api/topology"),
  getRunbooks: () => fetchJSON<RunbookListResponse>("/api/runbooks"),
  getRunbook: (name: string) =>
    fetchJSON<RunbookDetailResponse>(`/api/runbooks/${encodeURIComponent(name)}`),
  getHealth: () => fetchJSON<HealthResponse>("/api/health"),
  getStatus: () => fetchJSON<StatusResponse>("/api/status"),

  // 批二十八契约：Approvals
  getApprovals: (params?: { env?: string; status?: string; limit?: number; offset?: number }) =>
    fetchJSON<ApprovalsResponse>(
      `/api/approvals?${new URLSearchParams(
        cleanParams({ env: params?.env, status: params?.status, limit: params?.limit, offset: params?.offset }),
      )}`,
    ),
  approveApproval: (id: string, scope: ApprovalScope = "once") =>
    fetchJSON<{ status: string; scope?: string; error?: unknown }>(
      `/api/approvals/${encodeURIComponent(id)}/approve`,
      { method: "POST", body: JSON.stringify({ scope }) },
    ),
  denyApproval: (id: string) =>
    fetchJSON<{ status: string; error?: unknown }>(
      `/api/approvals/${encodeURIComponent(id)}/deny`,
      { method: "POST", body: "{}" },
    ),

  // 批二十八契约：Audit（运行轨迹，现成 JSON 化）
  getAuditSessions: (params?: { limit?: number; offset?: number }) =>
    fetchJSON<AuditSessionsResponse>(
      `/api/audit/sessions?${new URLSearchParams(cleanParams({ limit: params?.limit, offset: params?.offset }))}`,
    ),
  getAuditEvents: (params: { session_id: string; type?: string; limit?: number; offset?: number }) =>
    fetchJSON<AuditEventsResponse>(
      `/api/audit/events?${new URLSearchParams(
        cleanParams({ session_id: params.session_id, type: params.type, limit: params.limit, offset: params.offset }),
      )}`,
    ),
  getAuditEvent: (sessionId: string, seq: number) =>
    fetchJSON<AuditEvent>(`/api/audit/events/${encodeURIComponent(sessionId)}/${seq}`),
  deleteAuditEvents: (params: { session_id: string; older_than?: string }) =>
    fetchJSON<{ deleted?: number; error?: unknown }>(
      `/api/audit/events?${new URLSearchParams(cleanParams({ session_id: params.session_id, older_than: params.older_than }))}`,
      { method: "DELETE" },
    ),

  // 批二十八契约：Exec / Terminal
  runExec: (req: {
    command: string;
    host?: string;
    env?: string;
    session_id?: string;
    timeout_seconds?: number;
  }) =>
    fetchJSON<ExecResponse>("/api/exec", { method: "POST", body: JSON.stringify(req) }),
  getExec: (execId: string) => fetchJSON<ExecRecord>(`/api/exec/${encodeURIComponent(execId)}`),
  /** SSE 流：解析 exec:start / exec:output / exec:exit / exec:error 事件。 */
  execStream: (
    execId: string,
    onEvent: (event: { type: string; data: unknown }) => void,
    signal?: AbortSignal,
  ): Promise<void> => {
    const headers = new Headers();
    const token = typeof window !== "undefined" ? window.__HERMES_SESSION_TOKEN__ : undefined;
    if (token) headers.set("X-Hermes-Session-Token", token);
    return fetch(`${BASE}/api/exec/${encodeURIComponent(execId)}/stream`, {
      headers,
      signal,
    }).then(async (res) => {
      if (!res.ok || !res.body) {
        const text = await res.text().catch(() => "");
        throw parseErrorBody(text, res.status);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE 事件以空行分隔
        let idx: number;
        while ((idx = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          let eventType = "message";
          let data = "";
          for (const line of block.split("\n")) {
            if (line.startsWith("event:")) eventType = line.slice(6).trim();
            else if (line.startsWith("data:")) data += (data ? "\n" : "") + line.slice(5).trim();
          }
          if (data) {
            try {
              onEvent({ type: eventType, data: JSON.parse(data) });
            } catch {
              onEvent({ type: eventType, data });
            }
          }
        }
      }
    });
  },

  // 批二十八契约：Incidents（结构占位，返回空列表 + schema）
  getIncidents: (params?: { limit?: number; offset?: number }) =>
    fetchJSON<IncidentsResponse>(
      `/api/incidents?${new URLSearchParams(cleanParams({ limit: params?.limit, offset: params?.offset }))}`,
    ),

  // 批二十八契约：Sessions（活动 + 最近历史）
  getSessions: (params?: { limit?: number; offset?: number }) =>
    fetchJSON<SessionsResponse>(
      `/api/sessions?${new URLSearchParams(cleanParams({ limit: params?.limit, offset: params?.offset }))}`,
    ),
};

function cleanParams(p: Record<string, string | number | undefined>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(p)) {
    if (v !== undefined && v !== null && String(v) !== "") out[k] = String(v);
  }
  return out;
}
