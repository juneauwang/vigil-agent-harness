import { useMemo } from "react";
import { useNavigate } from "react-router";
import { Layers } from "lucide-react";
import type { TopologyView } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Lightweight topology schematic (方向 1：无光晕/3D/装饰图形).
 *
 * Renders a deterministic three-row SVG — cluster boxes → host boxes →
 * service dots — with thin 1px connector lines. Band-per-cluster keeps the
 * lines mostly vertical so the thumbnail reads as a topology at a glance.
 * Key-path services are amber. Clicking navigates to the topology page.
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
  const navigate = useNavigate();
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
      <div className="flex items-center gap-2 rounded-md border border-dashed border-border px-4 py-6 text-xs text-muted-foreground">
        <Layers className="size-4" />
        暂无主机数据
      </div>
    );
  }

  const line = "stroke-current stroke-opacity-25";

  return (
    <button
      type="button"
      onClick={() => navigate("/topology")}
      title="打开资产拓扑页"
      className={cn(
        "block w-full cursor-pointer rounded-md border border-border bg-card p-1 text-left transition-colors hover:border-primary/50",
        className,
      )}
    >
      <svg
        viewBox={`0 0 ${W} ${height}`}
        className="h-auto w-full"
        role="img"
        aria-label="拓扑缩略图：集群 / 主机 / 服务"
        preserveAspectRatio="xMidYMid meet"
      >
        <g className="text-muted-foreground">
          {/* cluster → host connectors */}
          {clusters.flatMap((c) =>
            c.hosts.map((h) => (
              <line
                key={`ch-${c.name}-${h.name}`}
                x1={c.cx}
                y1={CLUSTER_Y + CLUSTER_H}
                x2={h.cx}
                y2={HOST_Y}
                className={line}
              />
            )),
          )}
          {/* host → service connectors */}
          {clusters.flatMap((c) =>
            c.hosts.flatMap((h) =>
              h.services.map((s, i) => (
                <line
                  key={`hs-${c.name}-${h.name}-${i}`}
                  x1={h.cx}
                  y1={HOST_Y + HOST_H}
                  x2={s.cx}
                  y2={s.cy - SVC_R}
                  className={line}
                />
              )),
            ),
          )}
        </g>

        {/* clusters */}
        {clusters.map((c) => (
          <g key={`c-${c.name}`}>
            <rect
              x={c.cx - 44}
              y={CLUSTER_Y}
              width={88}
              height={CLUSTER_H}
              rx={4}
              className="fill-card stroke-current stroke-1"
            />
            <text
              x={c.cx}
              y={CLUSTER_Y + 15}
              textAnchor="middle"
              className="fill-current text-[9px] font-medium"
            >
              {c.name.length > 10 ? `${c.name.slice(0, 10)}…` : c.name}
            </text>
          </g>
        ))}

        {/* hosts */}
        {clusters.flatMap((c) =>
          c.hosts.map((h) => (
            <g key={`h-${h.name}`}>
              <rect
                x={h.cx - 4}
                y={HOST_Y}
                width={8}
                height={HOST_H}
                rx={2}
                className="fill-current stroke-none opacity-70"
              />
              <title>{h.name}</title>
            </g>
          )),
        )}

        {/* service dots — key-path services amber */}
        {clusters.flatMap((c) =>
          c.hosts.flatMap((h) =>
            h.services.map((s, i) => (
              <circle
                key={`s-${c.name}-${h.name}-${i}`}
                cx={s.cx}
                cy={s.cy}
                r={SVC_R}
                className={cn(
                  "stroke-none",
                  kpNames.has(s.name) ? "fill-amber-500" : "fill-current opacity-50",
                )}
              />
            )),
          ),
        )}
      </svg>
    </button>
  );
}
