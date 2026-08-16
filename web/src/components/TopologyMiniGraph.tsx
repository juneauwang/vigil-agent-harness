import { useMemo } from "react";
import type { TopologyView } from "@/lib/api";
import { cn } from "@/lib/ops";

/**
 * 轻量拓扑示意（只读总览，不可点击，点击联动为未来批次）：集群盒 →
 * 主机条 → 服务点 + 纤细连线。琥珀点 = 关键链路服务。节点仅保留悬停
 * 名称 tooltip（无 onClick / 无 pointer 光标 / 无 hover 高亮）；SVG
 * 保留原始宽度，窄容器横向滚动（避免等比压扁看不清）。
 */
const W = 960;
const CLUSTER_Y = 10;
const CLUSTER_H = 22;
const HOST_Y = 56;
const HOST_H = 14;
const SVC_Y = 92;
const SVC_R = 3;
const MAX_SERVICES = 48;

interface SvcNode {
  cx: number;
  cy: number;
  name: string;
}

interface HostNode {
  name: string;
  cx: number;
  services: SvcNode[];
}

interface ClusterNode {
  name: string;
  cx: number;
  hosts: HostNode[];
}

export function TopologyMiniGraph({
  view,
  className,
}: {
  view: TopologyView;
  className?: string;
}) {
  const kpNames = new Set(view.key_path_entity_names ?? []);

  const { clusters, height } = useMemo(() => {
    const clusters: ClusterNode[] = [];
    const grouped = new Map<string, TopologyView["hosts"]>();
    for (const host of view.hosts) {
      const c = host.card.cluster || "default";
      const list = grouped.get(c) ?? [];
      list.push(host);
      grouped.set(c, list);
    }
    const clusterNames = view.clusters.map((c) => c.name);
    for (const name of grouped.keys()) {
      if (!clusterNames.includes(name)) clusterNames.push(name);
    }

    const totalHosts = Math.max(1, view.hosts.length);
    const usable = W - 24;
    let xCursor = 12;
    for (const name of clusterNames) {
      const group = grouped.get(name) ?? [];
      const frac = Math.max(0.12, group.length / totalHosts);
      const bandW = Math.max(88, usable * frac);
      const cx = xCursor + bandW / 2;
      const cluster: ClusterNode = { name, cx, hosts: [] };
      xCursor += bandW;
      const innerPad = 10;
      const hostCount = Math.max(1, group.length);
      for (let hi = 0; hi < group.length; hi++) {
        const host = group[hi];
        const hx = xCursor - bandW + innerPad + ((hi + 0.5) * (bandW - 2 * innerPad)) / hostCount;
        const hostNode: HostNode = { name: host.card.name, cx: hx, services: [] };
        const svcs = host.services.slice(0, MAX_SERVICES);
        const svcCount = Math.max(1, svcs.length);
        for (let si = 0; si < svcs.length; si++) {
          const sx = xCursor - bandW + innerPad + ((si + 0.5) * (bandW - 2 * innerPad)) / svcCount;
          hostNode.services.push({ cx: sx, cy: SVC_Y, name: svcs[si].card.name });
        }
        cluster.hosts.push(hostNode);
      }
      clusters.push(cluster);
    }
    return { clusters, height: SVC_Y + SVC_R + 14 };
  }, [view]);

  if (view.hosts.length === 0) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-dashed border-[var(--vigil-border)] px-4 py-6 text-xs text-[var(--vigil-muted)]">
        暂无主机数据
      </div>
    );
  }

  return (
    <div
      className={cn(
        "scroll-thin overflow-x-auto rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)] p-1",
        className,
      )}
    >
      <svg
        viewBox={`0 0 ${W} ${height}`}
        className="h-auto w-full min-w-[720px]"
        role="img"
        aria-label="拓扑缩略图：集群 / 主机 / 服务"
        preserveAspectRatio="xMidYMid meet"
      >
        {/* connectors — 纤细 1px 连线 */}
        <g stroke="currentColor" strokeOpacity="0.25" className="text-[var(--vigil-muted)]">
          {clusters.flatMap((c) =>
            c.hosts.map((h) => (
              <line key={`ch-${c.name}-${h.name}`} x1={c.cx} y1={CLUSTER_Y + CLUSTER_H} x2={h.cx} y2={HOST_Y} />
            )),
          )}
          {clusters.flatMap((c) =>
            c.hosts.flatMap((h) =>
              h.services.map((s, i) => (
                <line
                  key={`hs-${c.name}-${h.name}-${i}`}
                  x1={h.cx}
                  y1={HOST_Y + HOST_H}
                  x2={s.cx}
                  y2={s.cy - SVC_R}
                />
              )),
            ),
          )}
        </g>

        {/* clusters — 卡片底 + 主色描边 */}
        {clusters.map((c) => (
          <g key={`c-${c.name}`}>
            <rect
              x={c.cx - 44}
              y={CLUSTER_Y}
              width={88}
              height={CLUSTER_H}
              rx={4}
              fill="var(--vigil-card)"
              stroke="var(--vigil-primary)"
              strokeOpacity="0.45"
              strokeWidth="1"
            />
            <text
              x={c.cx}
              y={CLUSTER_Y + 15}
              textAnchor="middle"
              fontSize="9"
              fontWeight="600"
              fill="var(--vigil-primary)"
            >
              {c.name.length > 10 ? `${c.name.slice(0, 10)}…` : c.name}
            </text>
            <title>{`集群 ${c.name}`}</title>
          </g>
        ))}

        {/* hosts — 主色浅条 */}
        {clusters.flatMap((c) =>
          c.hosts.map((h) => (
            <g key={`h-${h.name}`}>
              <rect
                x={h.cx - 4}
                y={HOST_Y}
                width={8}
                height={HOST_H}
                rx={2}
                fill="var(--vigil-primary)"
                opacity="0.55"
              />
              <title>{`主机 ${h.name}`}</title>
            </g>
          )),
        )}

        {/* service dots — 灰点，关键链路琥珀 */}
        {clusters.flatMap((c) =>
          c.hosts.flatMap((h) =>
            h.services.map((s, i) => (
              <circle
                key={`s-${c.name}-${h.name}-${i}`}
                cx={s.cx}
                cy={s.cy}
                r={SVC_R}
                fill={kpNames.has(s.name) ? "#f59e0b" : "var(--vigil-muted)"}
                opacity={kpNames.has(s.name) ? 1 : 0.55}
              >
                <title>{`服务 ${s.name}${kpNames.has(s.name) ? "（关键链路）" : ""}`}</title>
              </circle>
            )),
          ),
        )}
      </svg>
    </div>
  );
}
