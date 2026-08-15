/**
 * 开发期 mock 数据层（批二十八契约形状）。
 *
 * 契约验收红线：mock 只用文档 IP（203.0.113.x）与占位符，不写真实凭据。
 * 启用方式：localStorage `vigil-mock=1` 或 URL `?mock=1`。
 * 页面在显示 mock 数据时带「模拟数据」标识，避免与真实数据混淆。
 * 后端就绪后关闭 mock 即走真实 API。
 */

import type {
  ApprovalItem,
  AuditEvent,
  AuditSessionSummary,
  ExecRecord,
  ExecResponse,
  SessionSummary,
} from "@/lib/api";

export function isMockEnabled(): boolean {
  if (typeof window === "undefined") return false;
  try {
    if (new URLSearchParams(window.location.search).get("mock") === "1") return true;
    return localStorage.getItem("vigil-mock") === "1";
  } catch {
    return false;
  }
}

const NOW = Date.now();
const iso = (offsetMin: number) => new Date(NOW - offsetMin * 60_000).toISOString();

export const MOCK_APPROVALS: ApprovalItem[] = [
  {
    id: "apv_20260815_001",
    command: "kubectl -n prod rollout restart deploy/gateway-svc",
    description: "prod 变更确认门（B'）——L3 滚动发布",
    env: "prod",
    grade: "L3",
    session_key: "20260815_121956_6b88bf",
    source: "cli",
    status: "pending",
    created_at: iso(4),
    timeout_at: iso(-1),
    allow_session: false,
    allow_permanent: false,
  },
  {
    id: "apv_20260815_002",
    command: "docker restart harbor",
    description: "harbor 服务异常恢复（L2，prod 需审批）",
    env: "prod",
    grade: "L2",
    session_key: "20260815_121956_6b88bf",
    source: "web",
    status: "pending",
    created_at: iso(12),
    timeout_at: iso(-9),
    allow_session: false,
    allow_permanent: false,
  },
  {
    id: "apv_20260815_003",
    command: "ssh root@203.0.113.10 'systemctl status k3s'",
    description: "只读诊断（L1，审批通过记录）",
    env: "test",
    grade: "L1",
    session_key: "20260815_101234_1a2b3c",
    source: "cli",
    status: "approved",
    scope: "once",
    created_at: iso(95),
    timeout_at: null,
    allow_session: false,
    allow_permanent: false,
  },
];

export const MOCK_AUDIT_SESSIONS: AuditSessionSummary[] = [
  { session_id: "20260815_121956_6b88bf", event_count: 42, started_at: iso(200), ended_at: null },
  { session_id: "20260815_101234_1a2b3c", event_count: 17, started_at: iso(320), ended_at: iso(150) },
  { session_id: "20260814_223001_77aabb", event_count: 5, started_at: iso(900), ended_at: iso(860) },
];

export const MOCK_AUDIT_EVENTS: AuditEvent[] = [
  {
    seq: 1,
    ts: iso(199),
    type: "terminal",
    command: "vigil topo query host=node1",
    result: { exit_code: 0, output_preview: "node1  prod  k3s  control-plane  203.0.113.10" },
  },
  {
    seq: 2,
    ts: iso(198),
    type: "llm",
    command: "summarize topology",
    result: { exit_code: 0 },
  },
  {
    seq: 3,
    ts: iso(197),
    type: "tool",
    command: "topo_query",
    result: { exit_code: 0, output_preview: '{"ok": true}' },
  },
  {
    seq: 4,
    ts: iso(196),
    type: "approval",
    command: "kubectl -n prod rollout restart deploy/gateway-svc",
    result: { exit_code: 0, output_preview: "approval apv_20260815_001 created" },
  },
];

export const MOCK_SESSIONS: SessionSummary[] = [
  { session_id: "20260815_121956_6b88bf", title: "gateway-svc 发布", started_at: iso(200), last_activity_at: iso(1), model: "deepseek-v4-flash", running: true },
  { session_id: "20260815_101234_1a2b3c", title: "harbor 排查", started_at: iso(320), last_activity_at: iso(150), model: "deepseek-v4-flash", running: false },
  { session_id: "20260814_223001_77aabb", title: "拓扑同步", started_at: iso(900), last_activity_at: iso(860), model: "deepseek-v4-flash", running: false },
];

/** 模拟一次执行：按契约三态返回（文档 IP / 占位符）。 */
export function mockRunExec(command: string): ExecResponse {
  const lower = command.toLowerCase();
  if (lower.includes("delete") || lower.includes("rm -rf")) {
    return {
      status: "denied",
      code: "denied",
      reason: "该命令被安全策略拒绝（模拟数据）",
    };
  }
  if (lower.includes("restart") || lower.includes("rollout")) {
    return {
      status: "needs_approval",
      exec_id: "exec_001",
      approval_id: "apv_20260815_001",
      pending: true,
    };
  }
  return {
    status: "executed",
    exec_id: "exec_001",
    exit_code: 0,
    output:
      "$ " + command + "\n" +
      "node1  prod  k3s  control-plane  203.0.113.10\n" +
      "（模拟数据）",
  };
}

export function mockExecRecord(execId: string, command: string): ExecRecord {
  return {
    exec_id: execId,
    command,
    env: "prod",
    host: "203.0.113.10",
    status: "executed",
    exit_code: 0,
    output: "（模拟数据）command executed on 203.0.113.10",
    executed_at: iso(0),
    duration_ms: 312,
  };
}

/** 模拟 SSE 事件流（终端面板联调用）。 */
export async function mockExecStream(
  _execId: string,
  onEvent: (event: { type: string; data: unknown }) => void,
  signal?: AbortSignal,
): Promise<void> {
  const chunks = [
    { type: "exec:start", data: { exec_id: "exec_001" } },
    { type: "exec:output", data: { chunk: "$ vigil status\n" } },
    { type: "exec:output", data: { chunk: "gateway running · 203.0.113.10 · uptime 12h\n" } },
    { type: "exec:exit", data: { exit_code: 0 } },
  ];
  for (const ev of chunks) {
    if (signal?.aborted) return;
    onEvent(ev);
    await new Promise((r) => setTimeout(r, 120));
  }
}
