import { envClass, statusPillClass, statusDotClass, statusTone } from "@/lib/ops";
import { cn } from "@/lib/ops";

const ENV_LABEL: Record<string, string> = {
  prod: "生产环境",
  test: "测试环境",
  dev: "开发环境",
  local: "本地环境",
};

const TONE_LABEL: Record<string, string> = {
  ok: "在线/正常",
  warn: "告警",
  error: "故障",
  offline: "离线/未知",
};

/** 环境小标签（prod 红 / test 琥珀 / dev 橙 / local 蓝 / 其他灰），悬停说明。 */
export function EnvBadge({ env }: { env?: string }) {
  if (!env?.trim()) return null;
  const key = env.trim().toLowerCase();
  return (
    <span className={cn("vigil-env", envClass(env))} title={ENV_LABEL[key] ?? "环境"}>
      {env}
    </span>
  );
}

/** 状态 pill：文字（原始状态）+ 颜色双重表达，悬停说明语义。 */
export function StatusPill({ status }: { status?: string }) {
  if (!status?.trim()) return null;
  const tone = statusTone(status);
  return (
    <span
      className={cn("vigil-status-pill", statusPillClass(status))}
      title={`状态：${TONE_LABEL[tone] ?? "未知"}`}
    >
      <span className={cn("vigil-status-dot", statusDotClass(status))} />
      {status}
    </span>
  );
}

/** 纯状态点（链路摘要等紧凑场景用）：颜色 + 悬停说明；需文字时用 StatusPill。 */
export function StatusDot({ status }: { status?: string }) {
  const tone = statusTone(status);
  return (
    <span
      className={cn("vigil-status-dot", statusDotClass(status))}
      title={`状态：${TONE_LABEL[tone] ?? "未知"}`}
    />
  );
}

/** 状态文字标签（与 StatusDot 成对：点 + 字，双重表达）。 */
export function StatusText({ status }: { status?: string }) {
  const tone = statusTone(status);
  return <span className="text-[var(--vigil-muted)]">{TONE_LABEL[tone] ?? "未知"}</span>;
}
