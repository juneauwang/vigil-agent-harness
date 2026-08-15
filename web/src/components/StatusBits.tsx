import { envClass, statusPillClass, statusDotClass } from "@/lib/ops";
import { cn } from "@/lib/ops";

/** 环境小标签（prod 红 / test 琥珀 / dev 橙 / local 蓝 / 其他灰）。 */
export function EnvBadge({ env }: { env?: string }) {
  if (!env?.trim()) return null;
  return <span className={cn("vigil-env", envClass(env))}>{env}</span>;
}

/** 状态 pill（● 正常/告警/故障/离线）。 */
export function StatusPill({ status }: { status?: string }) {
  if (!status?.trim()) return null;
  return (
    <span className={cn("vigil-status-pill", statusPillClass(status))}>
      <span className={cn("vigil-status-dot", statusDotClass(status))} />
      {status}
    </span>
  );
}

/** 纯状态点（链路摘要等紧凑场景用）。 */
export function StatusDot({ status }: { status?: string }) {
  return <span className={cn("vigil-status-dot", statusDotClass(status))} />;
}
