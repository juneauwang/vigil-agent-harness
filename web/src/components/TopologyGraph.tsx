import { useCallback, useMemo } from "react";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { TopologyView } from "@/lib/api";
import {
  buildGraphModel,
  entityFromNode,
  kindLabel,
  MAX_GRAPH_NODES,
  nodeToneClass,
  type GraphEntityRef,
  type TopologyFlowNode,
} from "@/lib/topologyGraph";
import { lastSeenInfo } from "@/lib/ops";
import { EnvBadge, StatusDot } from "@/components/StatusBits";
import { cn } from "@/lib/ops";

/**
 * 可交互拓扑图（批三十五）：react-flow 缩放/平移 + 三层节点 + 状态着色 +
 * 点击节点 → 详情抽屉。数据来自现有 GET /api/topology，零新端点。
 * 集群 = 顶部标签栏；主机 = 名称 + 活性点 + 状态；服务 = label + 状态点；
 * 琥珀 = 关键链路实体（数据 key_paths 链上名字）。
 */

function ClusterNodeView({ data }: NodeProps<TopologyFlowNode>) {
  const { name, card, keyPath } = data;
  return (
    <div
      title={`${kindLabel("cluster")} ${name}${card.status ? ` · ${card.status}` : ""}`}
      className={cn(
        "flex h-9 w-[396px] items-center gap-2 rounded-md border px-2.5 text-xs font-semibold text-[var(--vigil-text)]",
        keyPath ? "border-amber-500/60 bg-amber-500/10" : "border-[var(--vigil-border)] bg-[var(--vigil-card)]",
      )}
    >
      <span className="truncate">🖥️ 集群 {name}</span>
      <EnvBadge env={card.env} />
      {card.status && <StatusDot status={card.status} />}
      {keyPath && <span className="text-[10px] text-amber-500">关键链路</span>}
    </div>
  );
}

function HostNodeView({ data }: NodeProps<TopologyFlowNode>) {
  const { name, card, keyPath } = data;
  const activity = lastSeenInfo(card.last_seen);
  return (
    <div
      title={`${kindLabel("host")} ${name}${card.status ? ` · ${card.status}` : ""} · ${activity.label}`}
      className={cn(
        "flex w-[180px] flex-col gap-0.5 rounded-md border px-2.5 py-2 text-xs",
        nodeToneClass(card.status, keyPath),
      )}
    >
      <div className="flex items-center gap-1.5">
        <StatusDot status={card.status} />
        <span className="min-w-0 truncate font-semibold text-[var(--vigil-text)]">{name}</span>
        <EnvBadge env={card.env} />
      </div>
      <div className="flex items-center gap-1.5 text-[10px] text-[var(--vigil-muted)]">
        <span
          className={cn(
            "size-1.5 shrink-0 rounded-full",
            activity.tone === "ok" ? "bg-[var(--vigil-ok)]" : "bg-[var(--vigil-offline)]",
          )}
        />
        <span className="truncate">{activity.label}</span>
      </div>
      {keyPath && <span className="text-[10px] text-amber-500">关键链路</span>}
    </div>
  );
}

function ServiceNodeView({ data }: NodeProps<TopologyFlowNode>) {
  const { name, card, hostName, keyPath } = data;
  return (
    <div
      title={`${kindLabel("service")} ${name}${hostName ? ` @ ${hostName}` : ""}${card.type ? ` · ${card.type}` : ""}${card.status ? ` · ${card.status}` : ""}`}
      className={cn(
        "flex w-[124px] items-center gap-1.5 rounded border px-2 py-1.5 text-[11px]",
        nodeToneClass(card.status, keyPath),
      )}
    >
      <StatusDot status={card.status} />
      <span className="min-w-0 truncate text-[var(--vigil-text)]">{name}</span>
    </div>
  );
}

function CrossHostNodeView({ data }: NodeProps<TopologyFlowNode>) {
  const { name, card, keyPath } = data;
  return (
    <div
      title={`${kindLabel("cross_host")} ${name}${card.type ? ` · ${card.type}` : ""}${card.status ? ` · ${card.status}` : ""}`}
      className={cn(
        "flex w-[180px] items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs",
        nodeToneClass(card.status, keyPath),
      )}
    >
      <StatusDot status={card.status} />
      <span className="min-w-0 truncate font-medium text-[var(--vigil-text)]">{name}</span>
      {card.type && <span className="truncate text-[10px] text-[var(--vigil-muted)]">{card.type}</span>}
      {keyPath && <span className="text-[10px] text-amber-500">关键链路</span>}
    </div>
  );
}

const nodeTypes = {
  cluster: ClusterNodeView,
  host: HostNodeView,
  service: ServiceNodeView,
  cross_host: CrossHostNodeView,
};

export default function TopologyGraph({
  view,
  onSelect,
  className,
}: {
  view: TopologyView;
  onSelect: (entity: GraphEntityRef) => void;
  className?: string;
}) {
  const model = useMemo(() => buildGraphModel(view), [view]);
  const handleNodeClick = useCallback((_e: unknown, node: TopologyFlowNode) => {
    onSelect(entityFromNode(node));
  }, [onSelect]);

  if (model.overflow) {
    return (
      <div className="flex h-[240px] items-center justify-center rounded-md border border-dashed border-[var(--vigil-border)] text-xs text-[var(--vigil-muted)]">
        节点数超过上限（{MAX_GRAPH_NODES}），请使用下方卡片/列表视图查看明细。
      </div>
    );
  }

  return (
    <div className={cn("h-[460px] w-full overflow-hidden rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)]", className)}>
      <ReactFlow
        nodes={model.nodes}
        edges={model.edges}
        nodeTypes={nodeTypes}
        onNodeClick={handleNodeClick}
        fitView
        minZoom={0.1}
        maxZoom={2}
        nodesDraggable={false}
        proOptions={{ hideAttribution: true }}
        className="topo-flow"
      >
        <Background gap={20} />
        <Controls />
        <MiniMap pannable zoomable />
      </ReactFlow>
    </div>
  );
}
