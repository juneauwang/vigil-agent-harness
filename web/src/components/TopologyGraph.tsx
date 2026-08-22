import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  useNodesState,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { TopologyView } from "@/lib/api";
import {
  buildGraphModel,
  clusterOptions,
  entityFromNode,
  filterGraphModel,
  kindLabel,
  layoutForceGraph,
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
 * 主机 = 名称 + 活性点 + 状态；服务 = label + 状态点；琥珀 = 依赖链路实体（services 层 depends_on）。
 *
 * 批四十九：布局改 d3-force 力导向（网络拓扑形态）；节点可拖（拖后固定，
 * 刷新/切集群回到力导向）；图上方集群标签栏（"全部" + 各集群）切换后节点
 * 重排 + fitView；``focusedName`` 高亮（列表 ↔ 图联动，TopologyPage 用）。
 */

function ClusterNodeView({ data }: NodeProps<TopologyFlowNode>) {
  const { name, card, keyPath, selected } = data;
  return (
    <div
      title={`${kindLabel("cluster")} ${name}${card.status ? ` · ${card.status}` : ""}`}
      className={cn(
        "flex h-9 w-[396px] items-center gap-2 rounded-md border px-2.5 text-xs font-semibold text-[var(--vigil-text)]",
        keyPath ? "border-amber-500/60 bg-amber-500/10" : "border-[var(--vigil-border)] bg-[var(--vigil-card)]",
        selected && "ring-2 ring-[var(--vigil-primary)]",
      )}
    >
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
      <span className="truncate">🖥️ 集群 {name}</span>
      <EnvBadge env={card.env} />
      {card.status && <StatusDot status={card.status} />}
      {keyPath && <span className="text-[10px] text-amber-500">依赖链路</span>}
    </div>
  );
}

function HostNodeView({ data }: NodeProps<TopologyFlowNode>) {
  const { name, card, keyPath, selected } = data;
  const activity = lastSeenInfo(card.last_seen);
  const port = portLabel(card);
  return (
    <div
      title={`${kindLabel("host")} ${name}${card.status ? ` · ${card.status}` : ""}${port ? ` · :${port}` : ""} · ${activity.label}`}
      className={cn(
        "flex w-[180px] flex-col gap-0.5 rounded-md border px-2.5 py-2 text-xs",
        nodeToneClass(card.status, keyPath),
        selected && "ring-2 ring-[var(--vigil-primary)]",
      )}
    >
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
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
      {port && (
        <div className="truncate font-mono text-[10px] text-[var(--vigil-muted)]">
          :{port}
        </div>
      )}
      {keyPath && <span className="text-[10px] text-amber-500">依赖链路</span>}
    </div>
  );
}

function ServiceNodeView({ data }: NodeProps<TopologyFlowNode>) {
  const { name, card, hostName, keyPath, selected } = data;
  const port = portLabel(card);
  return (
    <div
      title={`${kindLabel("service")} ${name}${hostName ? ` @ ${hostName}` : ""}${port ? ` · :${port}` : ""}${card.type ? ` · ${card.type}` : ""}${card.status ? ` · ${card.status}` : ""}`}
      className={cn(
        "flex w-[124px] items-center gap-1.5 rounded border px-2 py-1.5 text-[11px]",
        nodeToneClass(card.status, keyPath),
        selected && "ring-2 ring-[var(--vigil-primary)]",
      )}
    >
      <StatusDot status={card.status} />
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
      <span className="min-w-0 truncate text-[var(--vigil-text)]">{name}</span>
      {port && (
        <span className="shrink-0 font-mono text-[10px] text-[var(--vigil-muted)]">:{port}</span>
      )}
    </div>
  );
}

function CrossHostNodeView({ data }: NodeProps<TopologyFlowNode>) {
  const { name, card, keyPath, selected } = data;
  return (
    <div
      title={`${kindLabel("cross_host")} ${name}${card.type ? ` · ${card.type}` : ""}${card.status ? ` · ${card.status}` : ""}`}
      className={cn(
        "flex w-[180px] items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs",
        nodeToneClass(card.status, keyPath),
        selected && "ring-2 ring-[var(--vigil-primary)]",
      )}
    >
      <StatusDot status={card.status} />
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
      <span className="min-w-0 truncate font-medium text-[var(--vigil-text)]">{name}</span>
      {card.type && <span className="truncate text-[10px] text-[var(--vigil-muted)]">{card.type}</span>}
      {keyPath && <span className="text-[10px] text-amber-500">依赖链路</span>}
    </div>
  );
}

/** 端口标签：port 优先 + ports 去重合并，最多 3 个用 / 连接，空返回 null。 */
function portLabel(card: { port?: string | number | null; ports?: Array<number | string> | null }): string | null {
  const p = card.port != null && card.port !== "" ? [String(card.port)] : [];
  const ps = Array.isArray(card.ports)
    ? card.ports.filter((x) => x != null && x !== "").map(String)
    : [];
  const all = [...new Set([...p, ...ps])];
  if (all.length === 0) return null;
  return all.slice(0, 3).join("/") + (all.length > 3 ? "…" : "");
}

const nodeTypes = {
  cluster: ClusterNodeView,
  host: HostNodeView,
  service: ServiceNodeView,
  cross_host: CrossHostNodeView,
};

/**
 * 模块级状态缓存：TopologyPage 路由切换（拓扑→Overview→拓扑）会卸载/重挂载
 * TopologyGraph，内部 state（拖动位置/缩放/平移）随之丢失。这里在组件外缓存
 * 拖动位置与 viewport，重挂载时恢复——切换标签页回来，图保持用户最后的状态。
 * 注意：集群切换/力导向重算（组件内）仍走布局重置逻辑，缓存随 nodes 更新。
 */
let cachedNodePositions: Map<string, { x: number; y: number }> | null = null;
let cachedViewport: { x: number; y: number; zoom: number } | null = null;

export default function TopologyGraph({
  view,
  onSelect,
  onNodeFocus,
  focusedName,
  className,
}: {
  view: TopologyView;
  onSelect: (entity: GraphEntityRef) => void;
  /** 列表 ↔ 图联动：图中点选节点时回调（TopologyPage 联动列表滚动）。 */
  onNodeFocus?: (name: string) => void;
  /** 外部（列表选中）高亮图中的节点名。 */
  focusedName?: string | null;
  className?: string;
}) {
  // 批四十九：集群切换是前端过滤（不改 /api/topology 数据结构）。
  const options = useMemo(() => clusterOptions(view), [view]);
  const [cluster, setCluster] = useState<string>("all");
  // 力导向只在 view/cluster 变化时重算（节点选中只做轻量 data 映射，不重跑
  // 布局——点选联动列表时不抖动画布）。
  const baseModel = useMemo(() => {
    const base = buildGraphModel(view);
    const filtered = filterGraphModel(base, cluster);
    return layoutForceGraph(filtered);
  }, [view, cluster]);
  const model = useMemo(
    () => ({
      ...baseModel,
      nodes: baseModel.nodes.map((n) => ({
        ...n,
        data: { ...n.data, selected: focusedName != null && n.data.name === focusedName },
      })),
    }),
    [baseModel, focusedName],
  );
  // 批四十九修复：受控模式必须有 onNodesChange，否则拖动被 props 立即覆盖
  // （看起来拖不动）。useNodesState 内部 applyNodeChanges。
  // 初始值：重挂载时若有缓存位置则恢复（切标签页回来保持拖动状态），
  // 否则用力导向布局初始位置。useNodesState 只取首次值，后续由
  // onNodesChange/布局 effect 驱动。
  const initialNodes = useMemo(() => {
    if (cachedNodePositions) {
      return model.nodes.map((n) => {
        const p = cachedNodePositions!.get(n.id);
        return p ? { ...n, position: p } : n;
      });
    }
    return model.nodes;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [model]);
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  // 拖动/布局变化 → 同步缓存（模块级，卸载不丢）
  useEffect(() => {
    cachedNodePositions = new Map(nodes.map((n) => [n.id, n.position]));
  }, [nodes]);
  // 集群切换 → 力导向重算 → 重置布局。view 引用变化（切标签页重挂载/refetch）
  // 不触发重置——重挂载时 useNodesState 初始化已用缓存位置恢复，若这里再按
  // 新 view 重算力导向就会覆盖缓存（切回位置重置的根因）。
  // ⚠️ useEffect 挂载后必执行一次：必须跳过首次渲染，否则重挂载时这里仍会
  // 用新布局覆盖 useNodesState 从缓存恢复的位置（拓扑修改后切回依旧重置）。
  const isFirstLayout = useRef(true);
  useEffect(() => {
    if (isFirstLayout.current) {
      isFirstLayout.current = false;
      return;
    }
    setNodes(baseModel.nodes.map((n) => ({ ...n, data: { ...n.data, selected: false } })));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cluster, setNodes]);
  // focusedName 变化 → 只更新 selected 高亮，不动 position（不覆盖拖动）。
  useEffect(() => {
    setNodes((prev) =>
      prev.map((n) => {
        const fresh = model.nodes.find((m) => m.id === n.id);
        if (!fresh) return n;
        return { ...n, data: { ...n.data, selected: fresh.data.selected } };
      }),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [model, setNodes]);
  const handleNodeClick = useCallback(
    (_e: unknown, node: Node) => {
      const flowNode = node as unknown as TopologyFlowNode;
      onSelect(entityFromNode(flowNode));
      onNodeFocus?.(flowNode.data.name);
    },
    [onSelect, onNodeFocus],
  );
  // 平移/缩放结束 → 缓存 viewport（模块级，卸载不丢）
  const handleMoveEnd = useCallback(
    (_e: unknown, viewport: { x: number; y: number; zoom: number }) => {
      cachedViewport = viewport;
    },
    [],
  );

  if (model.overflow) {
    return (
      <div className="flex h-[240px] items-center justify-center rounded-md border border-dashed border-[var(--vigil-border)] text-xs text-[var(--vigil-muted)]">
        节点数超过上限（{MAX_GRAPH_NODES}），请使用下方卡片/列表视图查看明细。
      </div>
    );
  }

  return (
    <div className={cn("flex h-[460px] w-full flex-col overflow-hidden rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)]", className)}>
      {options.length > 1 && (
        <div className="flex flex-wrap items-center gap-1 border-b border-[var(--vigil-border)] px-2.5 py-1.5">
          <span className="mr-1 text-[10px] text-[var(--vigil-muted)]">集群</span>
          <button
            type="button"
            data-testid="topo-cluster-all"
            onClick={() => setCluster("all")}
            className={cn(
              "rounded px-2 py-0.5 text-[11px] transition-colors",
              cluster === "all"
                ? "bg-[var(--vigil-primary)] text-white"
                : "text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]",
            )}
          >
            全部
          </button>
          {options.map((name) => (
            <button
              key={name}
              type="button"
              data-testid={`topo-cluster-${name}`}
              onClick={() => setCluster(name)}
              className={cn(
                "rounded px-2 py-0.5 text-[11px] transition-colors",
                cluster === name
                  ? "bg-[var(--vigil-primary)] text-white"
                  : "text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]",
              )}
            >
              {name}
            </button>
          ))}
        </div>
      )}
      <ReactFlow
        // 集群切换 → 节点集合变化 → 重挂载触发 fitView（节点重排 + 适配）。
        key={cluster}
        nodes={nodes}
        onNodesChange={onNodesChange}
        edges={model.edges}
        nodeTypes={nodeTypes}
        onNodeClick={handleNodeClick}
        onMoveEnd={handleMoveEnd}
        // 重挂载（切标签页回来）时恢复用户最后的缩放/平移；无缓存才 fitView。
        defaultViewport={cachedViewport ?? undefined}
        fitView={cachedViewport == null}
        fitViewOptions={{ padding: 0.15 }}
        minZoom={0.1}
        maxZoom={2.5}
        nodesDraggable
        proOptions={{ hideAttribution: true }}
        className="topo-flow min-h-0 flex-1"
      >
        <Background gap={20} />
        <Controls />
        <MiniMap pannable zoomable />
      </ReactFlow>
    </div>
  );
}
