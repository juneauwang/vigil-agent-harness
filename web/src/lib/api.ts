/**
 * Vigil Console —— 精简数据层（复用后端 /api/*，凭据过滤不变）。
 *
 * 本批按「UI 壳批二十八 API 契约」（2026-08-15）接入 Approvals / Audit /
 * Incidents。后端由 Codex 实现，未就绪时端点 404 →
 * 页面按错误信封显示空态；开发期可用 mock 模式（localStorage vigil-mock=1
 * 或 ?mock=1）加载文档 IP 占位数据（lib/mock.ts）。
 */

declare global {
  interface Window {
    __VIGIL_BASE_PATH__?: string;
    __VIGIL_SESSION_TOKEN__?: string;
  }
}

function readBasePath(): string {
  if (typeof window === "undefined") return "";
  const raw = window.__VIGIL_BASE_PATH__ ?? "";
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
  const token = typeof window !== "undefined" ? window.__VIGIL_SESSION_TOKEN__ : undefined;
  if (token && !headers.has("X-Vigil-Session-Token")) {
    headers.set("X-Vigil-Session-Token", token);
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
  /** 批三十五：host 活性（lazy last_seen，epoch 秒；无记录不返回该字段）。 */
  last_seen?: number;
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
  status?: string;
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

export interface IncidentsResponse {
  incidents?: unknown[];
  total?: number;
  schema_version?: number;
  error?: { code?: string; message?: string; details?: Record<string, unknown> };
}

// ── 批三十一契约：对话 Session（/api/chat/*）─────────────────────────────

export interface ChatSessionSummary {
  id: string;
  title: string;
  created_at: string;
  busy: boolean;
  last_message_preview?: string;
}

export interface ChatSessionsResponse {
  sessions?: ChatSessionSummary[];
  total?: number;
  error?: { code?: string; message?: string; details?: Record<string, unknown> };
}

/** 历史消息（对齐前端 ChatMessage 的字段；tools 折叠进 assistant 气泡）。 */
export interface ChatHistoryMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  tools: {
    name: string;
    input_summary: string;
    output_summary?: string | null;
    ok?: boolean | null;
  }[];
  timestamp?: number | null;
}

export interface ChatHistoryResponse {
  chat_session_id?: string;
  messages?: ChatHistoryMessage[];
  total?: number;
  busy?: boolean;
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

  // 批二十八契约：Incidents（结构占位，返回空列表 + schema）
  getIncidents: (params?: { limit?: number; offset?: number }) =>
    fetchJSON<IncidentsResponse>(
      `/api/incidents?${new URLSearchParams(cleanParams({ limit: params?.limit, offset: params?.offset }))}`,
    ),

  // 批三十一契约：对话 Session（SSE 事件流：chat:delta/tool/tool_result/
  // approval_pending/done/error）
  createChatSession: () =>
    fetchJSON<{ chat_session_id: string; created_at?: string }>("/api/chat/sessions", {
      method: "POST",
      body: "{}",
    }),
  listChatSessions: () => fetchJSON<ChatSessionsResponse>("/api/chat/sessions"),
  /** 批三十六：中断当前 turn（对话页"停止"按钮）。会话不忙 → 409 not_busy。 */
  interruptChatSession: (sessionId: string) => {
    const headers = new Headers({ "Content-Type": "application/json" });
    const token = typeof window !== "undefined" ? window.__VIGIL_SESSION_TOKEN__ : undefined;
    if (token) headers.set("X-Vigil-Session-Token", token);
    return fetch(`${BASE}/api/chat/sessions/${encodeURIComponent(sessionId)}/interrupt`, {
      method: "POST",
      headers,
    }).then(async (res) => {
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw parseErrorBody(text, res.status);
      }
      return res.json().catch(() => ({}));
    });
  },
  /** 拉取会话历史消息（批三十三：切页/切回恢复现场）。 */
  getChatHistory: (sessionId: string) =>
    fetchJSON<ChatHistoryResponse>(`/api/chat/sessions/${encodeURIComponent(sessionId)}/messages`),
  /** 发消息：POST 后消费 SSE 流（与批二十八 exec SSE 同款解析）。 */
  chatStream: (
    sessionId: string,
    message: string,
    onEvent: (event: { type: string; data: unknown }) => void,
    signal?: AbortSignal,
  ): Promise<void> => {
    const headers = new Headers({ "Content-Type": "application/json" });
    const token = typeof window !== "undefined" ? window.__VIGIL_SESSION_TOKEN__ : undefined;
    if (token) headers.set("X-Vigil-Session-Token", token);
    return fetch(`${BASE}/api/chat/sessions/${encodeURIComponent(sessionId)}/messages`, {
      method: "POST",
      headers,
      body: JSON.stringify({ message }),
      signal,
    }).then(async (res) => {
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw parseErrorBody(text, res.status);
      }
      if (!res.body) return;
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
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
};

function cleanParams(p: Record<string, string | number | undefined>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(p)) {
    if (v !== undefined && v !== null && String(v) !== "") out[k] = String(v);
  }
  return out;
}
