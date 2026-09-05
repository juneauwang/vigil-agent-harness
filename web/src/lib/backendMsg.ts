/**
 * Backend-message translation map (Option C): the dashboard backend keeps
 * emitting zh (its default language); the frontend maps known backend zh
 * messages to the current UI language before rendering. Unknown messages
 * pass through unchanged — zh stays zh for anything the map doesn't cover.
 *
 * Matching semantics mirror the ops.ts matchers: `text.includes(pattern)`
 * against known zh substrings, first match wins. Prefix entries preserve
 * both the head (e.g. an "[code] " error prefix) and the tail (dynamic
 * detail after the matched span). Regex entries match whole messages with
 * capture groups for interpolated messages.
 *
 * Keep in sync with the backend emitters:
 * - hermes_cli/monitoring.py (PromQL / Alertmanager error families)
 * - tools/matrix_data.py (coverage warnings, shown on MatrixPage)
 * - tools/approval.py web-approval registry messages
 */
import type { Lang } from "@/i18n";
import i18n from "@/i18n";

interface PrefixEntry {
  /** zh substring to match. */
  pat: string;
  /** backend.* i18n key for the matched span. */
  key: string;
}

interface RegexEntry {
  /** whole-message regex with capture groups. */
  re: RegExp;
  /** backend.* i18n key; capture groups passed as {{v1}}…{{v4}}. */
  key: string;
}

const PREFIX_ENTRIES: PrefixEntry[] = [
  // ── monitoring: Prometheus / PromQL ─────────────────────────────────────
  { pat: "未配置 Prometheus（config ops.prometheus.endpoint），仅健康探测可用。", key: "backend.promUnconfigured" },
  { pat: "Prometheus 请求超时: ", key: "backend.promTimeout" },
  { pat: "Prometheus 请求失败: ", key: "backend.promFailed" },
  { pat: "Prometheus 查询失败 HTTP ", key: "backend.promHttpFailed" },
  { pat: "Prometheus 返回非 JSON: ", key: "backend.promNonJson" },
  { pat: "Prometheus 查询错误: ", key: "backend.promQueryError" },
  { pat: "Prometheus 响应异常 status=", key: "backend.promBadStatus" },
  { pat: "Prometheus 响应缺少 result 列表。", key: "backend.promNoResult" },
  { pat: "PromQL 预校验失败：", key: "backend.promqlPreflightFailed" },
  { pat: "promql 必填（PromQL 表达式）。", key: "backend.promqlRequired" },
  { pat: "duration/step 格式非法（应为 30s/1m/1h/24h 等时长）。", key: "backend.durationInvalid" },
  // ── monitoring: Alertmanager ─────────────────────────────────────────────
  { pat: "未配置 Alertmanager（config ops.prometheus.alertmanager），仅健康探测可用。", key: "backend.alertmanagerUnconfigured" },
  { pat: "Alertmanager 请求超时: ", key: "backend.amTimeout" },
  { pat: "Alertmanager 请求失败: ", key: "backend.amFailed" },
  { pat: "Alertmanager 查询失败 HTTP ", key: "backend.amHttpFailed" },
  { pat: "Alertmanager 返回非 JSON: ", key: "backend.amNonJson" },
  { pat: "Alertmanager 响应不是告警列表。", key: "backend.amBadShape" },
  // ── approvals (web-approval registry messages) ──────────────────────────
  { pat: "审批不存在: ", key: "backend.approvalNotFound" },
  { pat: "审批已超时（wait 策略：不自动批准，命令保持 pending）", key: "backend.approvalTimeoutApprove" },
  { pat: "审批已超时（wait 策略：不自动拒绝，命令保持 pending）", key: "backend.approvalTimeoutDeny" },
  { pat: "scope 必须是 once/session/permanent", key: "backend.approvalBadScope" },
  { pat: "该审批不支持 session 作用域（prod 变更确认门只允许 once）", key: "backend.approvalScopeSession" },
  { pat: "该审批不支持 permanent 作用域", key: "backend.approvalScopePermanent" },
];

const REGEX_ENTRIES: RegexEntry[] = [
  // matrix_data coverage warnings (MatrixPage warnings block) — interpolated.
  {
    re: /^matrix\.(?<env>\S+) 漏配 (?<n>\d+) 个动作（(?<shown>[^）]*)）——默认 approve（保守）。$/,
    key: "backend.matrixMissing",
  },
  {
    re: /^sources\.(?<env>\S+)\.(?<act>\S+): 来源 (?<source>.+) 未知，按原样保留。$/,
    key: "backend.matrixSourceUnknown",
  },
];

/**
 * Translate a backend-returned message into the target UI language.
 * zh passes through untouched (the backend already emits zh); en maps known
 * zh messages via the table below; anything unknown passes through as-is.
 */
export function translateBackendMessage(msg: string, lang: Lang): string {
  if (!msg || lang !== "en") return msg;
  for (const entry of PREFIX_ENTRIES) {
    const idx = msg.indexOf(entry.pat);
    if (idx !== -1) {
      // Preserve any head (e.g. "[code] ") and the dynamic tail after the span.
      return msg.slice(0, idx) + i18n.t(entry.key) + msg.slice(idx + entry.pat.length);
    }
  }
  for (const entry of REGEX_ENTRIES) {
    const m = entry.re.exec(msg);
    if (m && m.groups) {
      return i18n.t(entry.key, m.groups);
    }
  }
  return msg;
}
