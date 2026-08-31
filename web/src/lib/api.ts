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
  /** 批三十七 §Y：监听端口列表（实体 schema v0.3 可选字段，如 [9090, 443]）。 */
  ports?: Array<number | string>;
  /** v0.4 服务行字段：managed_by/extra_ports/log_paths/depends_on（图连线用）。 */
  managed_by?: string;
  extra_ports?: Array<number | string>;
  log_paths?: string[];
  depends_on?: string[];
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

/** 批八十五：清空全部拓扑数据（POST /api/topology/reset）返回统计。 */
export interface TopologyResetResult {
  ok?: boolean;
  error?: string;
  removed?: string[];
  hosts?: number;
  clusters?: number;
  entities?: number;
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

/** YAPL P4：runbook 执行（交互触发）+ 执行历史（事后审计视图）。 */
export interface RunbookStepResult {
  id?: string;
  action?: string;
  status?: string;
  ok?: boolean;
  error?: string;
  commands?: Array<{ desc?: string; command?: string }>;
  steps?: RunbookStepResult[];
}

export interface RunbookExecution {
  ts?: string;
  runbook?: string;
  env?: string;
  source?: string;
  trigger_context?: Record<string, unknown>;
  result?: string;
  error?: string;
  rolled_back?: boolean;
  steps?: RunbookStepResult[];
  duration_s?: number;
  operator?: string;
  /** 批八十：进度流关联 id（web 触发的执行带；SSE 入口）。 */
  exec_id?: string;
}

export interface RunbookExecutionsResponse {
  ok: boolean;
  error?: string;
  data?: {
    count: number;
    executions: RunbookExecution[];
    /** 批八十：本进程正在执行中的流（UI 显示"运行中" + 展开实时步骤）。 */
    running?: RunbookRunningExec[];
    /** 批八十一：执行级并发锁快照（同名 runbook / 同目标禁止并发下发）。 */
    locks?: RunbookLock[];
  };
}

/** 批八十一：执行级并发锁（引擎入口注册；覆盖 web/定时/LLM 全部入口）。 */
export interface RunbookLock {
  exec_id: string;
  runbook?: string;
  version?: string;
  env?: string;
  targets?: string[];
  started_at?: string;
}

export interface RunbookRunningExec {
  exec_id: string;
  runbook?: string;
  env?: string;
  started_at?: string;
}

/** 批八十：执行启动响应（立即返回 exec_id；进度走 SSE，终态经 runbook_done）。 */
export interface RunbookRunResponse {
  ok: boolean;
  error?: string;
  data?: {
    exec_id: string;
    runbook?: string;
    env?: string;
    started_at?: string;
    status?: string;
  };
}

/** 批八十：runbook 执行进度事件（SSE data.type）。 */
export interface RunbookProgressEvent {
  type: "step_start" | "step_done" | "step_failed" | "rollback_start" | "rollback_done" | "runbook_done";
  exec_id?: string;
  runbook?: string;
  version?: string;
  step_id?: string;
  title?: string;
  action?: string;
  target?: string;
  status?: string;
  ts?: string;
  detail?: string;
  phase?: string;
  error?: string;
  rolled_back?: boolean;
  duration_s?: number;
  step_count?: number;
}

/** 批八十一：runbook 覆盖率载荷（/api/runbook/coverage）。 */
export interface RunbookCoverageResponse {
  ok: boolean;
  error?: string;
  data?: {
    generated_at?: string;
    high_risk: {
      high_risk?: string[];
      total: number;
      covered: number;
      uncovered: string[];
      coverage_pct: number;
    };
    usage: {
      window_days: number;
      audit_events_scanned: number;
      actions: {
        action: string;
        use_count: number;
        covered: boolean;
        runbooks: string[];
      }[];
      total_unique: number;
      covered_unique: number;
      coverage_pct: number;
      gaps: {
        action: string;
        use_count: number;
        covered: boolean;
        runbooks: string[];
      }[];
    };
  };
}

/** 操作矩阵（YAPL §11）：matrix/sources 均为 {env: {action: 值}}。 */
export type MatrixLevel = "execute" | "approve" | "required";

export interface MatrixData {
  schema_version?: number;
  updated_at?: string;
  source?: string;
  base_template?: string;
  matrix: Record<string, Record<string, string>>;
  sources: Record<string, Record<string, string>>;
  warnings?: string[];
  path?: string;
  /** 动作词表（schemas.yaml actions，行枚举源） */
  actions?: string[];
}

export interface MatrixResponse {
  ok: boolean;
  error?: string;
  data?: MatrixData;
}

export interface MatrixInitSelections {
  execute: string[];
  approve: string[];
}

export interface MatrixInitResponse {
  ok: boolean;
  error?: string;
  data?: MatrixData;
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

/** 批五十：/api/incidents 条目（watch inbox 告警，source=alertmanager）。 */
export interface IncidentItem {
  alertname?: string;
  severity?: string;
  instance?: string;
  startsAt?: string;
  state?: string;
  collected_at?: string;
  processed?: boolean;
  source?: string;
}

export interface IncidentsResponse {
  incidents?: IncidentItem[];
  total?: number;
  schema_version?: number;
  limit?: number;
  offset?: number;
  has_more?: boolean;
  error?: { code?: string; message?: string; details?: Record<string, unknown> };
}

// ── 批三十一契约：对话 Session（/api/chat/*）─────────────────────────────

/** 批八十一：会话 context 使用率（used/limit/pct/model；用量或上限拿不到 →
 * null，前端显示"—"，不瞎猜）。 */
export interface ChatContextUsage {
  used_tokens: number | null;
  limit_tokens: number | null;
  pct: number | null;
  model?: string | null;
}

export interface ChatSessionSummary {
  id: string;
  title: string;
  created_at: string;
  busy: boolean;
  last_message_preview?: string;
  /** 批四十一 §8：会话级模型（缺省 = 配置默认）。 */
  model?: string;
  /** 批八十一：当前 context 用量（messages active=1 累计 + 模型上限三级解析）。 */
  context_usage?: ChatContextUsage | null;
}

/** 批四十一 §8：可选模型目录项（GET /api/models，静态目录 + 配置默认，
 * 零敏感信息；选项严格来自该接口，前端不硬编码）。 */
export interface ChatModelOption {
  id: string;
  name: string;
  description?: string;
  /** 用途标注（目录派生；批四十二 §AY 起前端不再渲染，字段保留兼容）。 */
  tag?: string;
  /** 配置中当前默认模型。 */
  default?: boolean;
  /** 路由 provider（批五十一：custom:<slug> / provider 名 / 空）；切换时随 model 提交。 */
  provider?: string;
}

export interface ChatModelsResponse {
  models?: ChatModelOption[];
  provider?: string;
  default_model?: string;
  error?: { code?: string; message?: string; details?: Record<string, unknown> };
}

export interface ChatSessionsResponse {
  sessions?: ChatSessionSummary[];
  total?: number;
  error?: { code?: string; message?: string; details?: Record<string, unknown> };
}

/** 批四十二 §BJ：历史推理的结构化归属步（单工具调用消息的推理）。 */
export interface ChatHistoryReasoningStep {
  /** 服务端工具调用 id（与该消息 tools[].tool_id 对齐）。 */
  tool_id?: string;
  text?: string;
}

/** 批四十二 §BJ：历史推理结构——单值字符串（旧格式/消息级）或
 * {steps:[{tool_id,text}]}（按工具步挂载）。前端两种都读。 */
export type ChatHistoryReasoning = string | { steps?: ChatHistoryReasoningStep[] };

/** 历史消息（对齐前端 ChatMessage 的字段；tools 折叠进 assistant 气泡）。 */
export interface ChatHistoryMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  /** 批四十一 §3：推理过程纯文本（默认折叠展示）；批四十二起可结构化归属。 */
  reasoning?: ChatHistoryReasoning;
  tools: {
    name: string;
    input_summary: string;
    output_summary?: string | null;
    ok?: boolean | null;
    /** 批四十一 §5：服务端工具调用唯一 id（空串=旧格式）。 */
    tool_id?: string | null;
  }[];
  timestamp?: number | null;
  /** 批八十二：历史审批卡（服务端折叠进 assistant 气泡；刷新/切回恢复）。 */
  approvals?: {
    approval_id: string;
    command: string;
    description: string;
    env: string;
    grade?: string | null;
    timeout_at?: string | null;
    status: "pending" | "approved" | "denied" | "error";
  }[];
  /** 批八十二：历史 clarify 卡。 */
  clarifies?: {
    clarify_id: string;
    question: string;
    choices: string[] | null;
    multi_select: boolean;
    timeout_at?: string | null;
    status: "pending" | "answered" | "timed_out" | "error";
  }[];
}

export interface ChatHistoryResponse {
  chat_session_id?: string;
  messages?: ChatHistoryMessage[];
  total?: number;
  busy?: boolean;
  error?: { code?: string; message?: string; details?: Record<string, unknown> };
}

// ── 批六十四契约：chat 用量（当前会话实时 token + 价格三级来源费用）──

export interface ChatUsagePrice {
  input_per_1m: number;
  output_per_1m: number;
  /** usd | cny（不跨币种换算，显示原币种符号 $ / ¥）。 */
  currency: string;
  /** manual（手动覆盖）| online（在线拉取）| builtin（内置兜底）。 */
  source: string;
  fetched_at?: string | null;
  pricing_version?: string | null;
}

export interface ChatUsageResponse {
  ok?: boolean;
  session_id: string;
  model?: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  checked_at?: string;
  /** 价格可用时按三级价格估算；不可用为 null（UI 只显 token 不瞎算费用）。 */
  cost?: number | null;
  cost_currency?: string | null;
  price_source?: string | null;
  price?: ChatUsagePrice | null;
  error?: { code?: string; message?: string };
}

/** 历史累计（复用 /api/analytics/usage：daily 按天 + totals 区间汇总）。 */
export interface UsageAnalyticsDaily {
  day: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  reasoning_tokens: number;
  estimated_cost: number;
  actual_cost: number;
  sessions: number;
  api_calls: number;
}

export interface UsageAnalyticsTotals {
  total_input: number;
  total_output: number;
  total_cache_read: number;
  total_reasoning: number;
  total_estimated_cost: number;
  total_actual_cost: number;
  total_sessions: number;
  total_api_calls: number;
}

export interface UsageAnalyticsResponse {
  daily?: UsageAnalyticsDaily[];
  totals?: UsageAnalyticsTotals;
  period_days?: number;
  error?: { code?: string; message?: string };
}

// ── UI 监控（OPS-DELTA #78）：健康 / PromQL 查询 / 活跃告警 ──────────────

export interface MonitoringHealthService {
  name: string;
  host?: string;
  cluster?: string;
  type?: string;
  managed_by?: string;
  env?: string;
  endpoint?: string | null;
  status: "up" | "down" | "unknown";
  latency_ms?: number | null;
  checked_at?: string;
  ports?: Array<{
    port?: number | null;
    proto?: string;
    status: string;
    latency_ms?: number;
  }>;
}

export interface MonitoringHealthResponse {
  ok?: boolean;
  data?: {
    checked_at?: string;
    cached?: boolean;
    summary?: { up: number; down: number; unknown: number };
    services?: MonitoringHealthService[];
  };
}

export interface MonitoringSeries {
  name: string;
  labels: Record<string, string>;
  points: Array<[number, number | null]>;
  summary: { min?: number; max?: number; last?: number };
  point_count: number;
}

export interface MonitoringQueryResponse {
  ok?: boolean;
  data?: {
    kind: string;
    query: string;
    duration: string;
    step: string;
    series: MonitoringSeries[];
    truncated?: boolean;
  };
}

export interface MonitoringAlert {
  alertname: string;
  severity: string;
  instance?: string;
  labels?: Record<string, string>;
  startsAt?: string;
  state?: string;
}

export interface MonitoringAlertsResponse {
  ok?: boolean;
  data?: { alerts: MonitoringAlert[]; count: number };
}

// ── API methods ────────────────────────────────────────────────────────────

export const api = {
  // 第一批：拓扑 / runbook / 状态 / 健康
  getTopology: () => fetchJSON<TopologyResponse>("/api/topology"),
  /** 批八十五：清空全部拓扑数据（破坏性；确认对话框由页面负责）。 */
  resetTopology: () =>
    fetchJSON<{ ok: boolean; data?: TopologyResetResult; error?: unknown }>(
      "/api/topology/reset",
      { method: "POST", body: JSON.stringify({ confirm: true }) },
    ),
  getRunbooks: () => fetchJSON<RunbookListResponse>("/api/runbooks"),
  getRunbook: (name: string) =>
    fetchJSON<RunbookDetailResponse>(`/api/runbooks/${encodeURIComponent(name)}`),
  // YAPL P4：v0.2 runbook 执行（dashboard 触发，审批门走 web 注册表）+ 历史
  getRunbookExecutions: (limit = 20) =>
    fetchJSON<RunbookExecutionsResponse>(
      `/api/runbook/executions?limit=${encodeURIComponent(String(limit))}`,
    ),
  runRunbook: (name: string, env?: string, triggerContext?: Record<string, unknown>) =>
    fetchJSON<RunbookRunResponse>("/api/runbook/executions", {
      method: "POST",
      body: JSON.stringify({ name, env, trigger_context: triggerContext }),
    }),
  /** 批八十：runbook 执行进度 SSE（fetch + reader，可带 token 头；断开由
   * 调用方 abort，服务端流照常结束）。事件对象经 onEvent 回调（type 为
   * step_start/step_done/step_failed/rollback_start/rollback_done/
   * runbook_done）。 */
  runbookProgressStream: (
    execId: string,
    onEvent: (event: RunbookProgressEvent) => void,
    signal?: AbortSignal,
  ): Promise<void> => {
    const headers = new Headers({ "Content-Type": "application/json" });
    const token = typeof window !== "undefined" ? window.__VIGIL_SESSION_TOKEN__ : undefined;
    if (token) headers.set("X-Vigil-Session-Token", token);
    return fetch(`${BASE}/api/runbook/executions/${encodeURIComponent(execId)}/progress`, {
      headers,
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
          let data = "";
          for (const line of block.split("\n")) {
            if (line.startsWith("data:")) data += (data ? "\n" : "") + line.slice(5).trim();
          }
          if (data) {
            try {
              onEvent(JSON.parse(data) as RunbookProgressEvent);
            } catch {
              // 忽略无法解析的帧（保持流不中断）。
            }
          }
        }
      }
    });
  },
  /** 批八十一：runbook 覆盖率（确定性高危覆盖率 + 审计动作使用率）。 */
  getRunbookCoverage: () => fetchJSON<RunbookCoverageResponse>("/api/runbook/coverage"),
  // YAPL P3：操作矩阵（人工安全资产，修改即审计；LLM 只有 matrix_query 只读）
  getMatrix: () => fetchJSON<MatrixResponse>("/api/matrix"),
  setMatrixCell: (env: string, action: string, level: MatrixLevel) =>
    fetchJSON<{ ok: boolean; changed?: boolean; error?: unknown }>(
      "/api/matrix",
      { method: "PUT", body: JSON.stringify({ env, action, level }) },
    ),
  initMatrix: (template: string, selections?: MatrixInitSelections, force = false) =>
    fetchJSON<MatrixInitResponse>(
      "/api/matrix/init",
      { method: "POST", body: JSON.stringify({ template, selections, force }) },
    ),
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
  // 批四十九：web chat clarify 应答（body {answer: string | string[]}；
  // 多选传数组，取消/跳过传空串 → agent 回合继续）。
  answerChatClarify: (sessionId: string, answer: string | string[]) =>
    fetchJSON<{ status: string; clarify_id?: string; error?: unknown }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}/clarify`,
      { method: "POST", body: JSON.stringify({ answer }) },
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

  // 批五十契约：Incidents（读 watch inbox 返回告警列表，schema_version=2）
  getIncidents: (params?: { limit?: number; offset?: number }) =>
    fetchJSON<IncidentsResponse>(
      `/api/incidents?${new URLSearchParams(cleanParams({ limit: params?.limit, offset: params?.offset }))}`,
    ),
  // UI 监控 API（OPS-DELTA #78）：健康 / PromQL 查询 / 活跃告警
  getMonitoringHealth: (refresh = false) =>
    fetchJSON<MonitoringHealthResponse>(
      `/api/monitoring/health${refresh ? "?refresh=1" : ""}`,
    ),
  queryMonitoring: (promql: string, duration?: string, step?: string) =>
    fetchJSON<MonitoringQueryResponse>(
      `/api/monitoring/query?${new URLSearchParams(
        cleanParams({ promql, duration, step }),
      )}`,
    ),
  getMonitoringAlerts: () =>
    fetchJSON<MonitoringAlertsResponse>("/api/monitoring/alerts"),

  // 批三十一契约：对话 Session（SSE 事件流：chat:delta/tool/tool_result/
  // approval_pending/done/error）
  createChatSession: (model?: string, provider?: string) =>
    fetchJSON<{ chat_session_id: string; created_at?: string; model?: string }>("/api/chat/sessions", {
      method: "POST",
      body: JSON.stringify(model ? { model, ...(provider ? { provider } : {}) } : {}),
    }),
  /** 批四十一 §8：可选模型目录（静态目录 + 配置默认）。 */
  getModels: () => fetchJSON<ChatModelsResponse>("/api/models"),
  /** 批四十一 §8：切换会话模型（会话级生效，新消息生效）。 */
  setChatSessionModel: (sessionId: string, model: string, provider?: string) =>
    fetchJSON<{ chat_session_id: string; model: string }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}/model`,
      { method: "POST", body: JSON.stringify({ model, ...(provider ? { provider } : {}) }) },
    ),
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
  /** 批六十四：当前会话实时 token + 费用（价格三级来源，不可用 cost null）。 */
  getChatUsage: (sessionId: string) =>
    fetchJSON<ChatUsageResponse>(`/api/chat/usage?${new URLSearchParams({ session_id: sessionId })}`),
  /** 批六十四：历史累计（今天取 daily 末行；近 30 天取 totals）。 */
  getUsageAnalytics: (days = 30) =>
    fetchJSON<UsageAnalyticsResponse>(`/api/analytics/usage?${new URLSearchParams({ days: String(days) })}`),
};

function cleanParams(p: Record<string, string | number | undefined>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(p)) {
    if (v !== undefined && v !== null && String(v) !== "") out[k] = String(v);
  }
  return out;
}
