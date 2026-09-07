import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import "@/i18n";
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
import { lastSeenInfo, matchesSearch } from "@/lib/ops";
import { EnvBadge, StatusDot } from "@/components/StatusBits";
import { cn } from "@/lib/ops";

/**
 * Interactive topology graph (batch 35): react-flow zoom/pan + three node
 * layers + status coloring + click node → detail drawer. Data comes from the
 * existing GET /api/topology, zero new endpoints.
 * Host = name + activity dot + status; service = label + status dot; amber =
 * dependency-path entity (services-level depends_on).
 *
 * Batch 49: layout switched to d3-force (network-topology shape); nodes are
 * draggable (pinned after drag; refresh/cluster switch returns to force
 * layout); cluster tab bar above the graph ("all" + each cluster) re-lays out
 * nodes + fitView; ``focusedName`` highlight (list ↔ graph sync, used by
 * TopologyPage).
 */

function ClusterNodeView({ data }: NodeProps<TopologyFlowNode>) {
  const { t } = useTranslation();
  const { name, card, keyPath, selected, dim } = data;
  return (
    <div
      title={`${kindLabel("cluster")} ${name}${card.status ? ` · ${card.status}` : ""}`}
      className={cn(
        "flex h-9 w-[396px] items-center gap-2 rounded-md border px-2.5 text-xs font-semibold text-[var(--vigil-text)]",
        keyPath ? "border-amber-500/60 bg-amber-500/10" : "border-[var(--vigil-border)] bg-[var(--vigil-card)]",
        selected && "ring-2 ring-[var(--vigil-primary)]",
        dim && "opacity-30",
      )}
    >
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
      <span className="truncate">{t("topology.nodeCluster", { name })}</span>
      <EnvBadge env={card.env} />
      {card.status && <StatusDot status={card.status} />}
      {keyPath && <span className="text-[10px] text-amber-500">{t("topology.keyPathBadge")}</span>}
    </div>
  );
}

function HostNodeView({ data }: NodeProps<TopologyFlowNode>) {
  const { t } = useTranslation();
  const { name, card, keyPath, selected, dim } = data;
  const activity = lastSeenInfo(card.last_seen);
  const port = portLabel(card);
  return (
    <div
      title={`${kindLabel("host")} ${name}${card.status ? ` · ${card.status}` : ""}${port ? ` · :${port}` : ""} · ${activity.label}`}
      className={cn(
        "flex w-[180px] flex-col gap-0.5 rounded-md border px-2.5 py-2 text-xs",
        nodeToneClass(card.status, keyPath),
        selected && "ring-2 ring-[var(--vigil-primary)]",
        dim && "opacity-30",
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
      {keyPath && <span className="text-[10px] text-amber-500">{t("topology.keyPathBadge")}</span>}
    </div>
  );
}

function ServiceNodeView({ data }: NodeProps<TopologyFlowNode>) {
  const { name, card, hostName, keyPath, selected, dim } = data;
  const port = portLabel(card);
  return (
    <div
      title={`${kindLabel("service")} ${name}${hostName ? ` @ ${hostName}` : ""}${port ? ` · :${port}` : ""}${card.type ? ` · ${card.type}` : ""}${card.status ? ` · ${card.status}` : ""}`}
      className={cn(
        "flex w-[124px] items-center gap-1.5 rounded border px-2 py-1.5 text-[11px]",
        nodeToneClass(card.status, keyPath),
        selected && "ring-2 ring-[var(--vigil-primary)]",
        dim && "opacity-30",
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
  const { t } = useTranslation();
  const { name, card, keyPath, selected, dim } = data;
  return (
    <div
      title={`${kindLabel("cross_host")} ${name}${card.type ? ` · ${card.type}` : ""}${card.status ? ` · ${card.status}` : ""}`}
      className={cn(
        "flex w-[180px] items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs",
        nodeToneClass(card.status, keyPath),
        selected && "ring-2 ring-[var(--vigil-primary)]",
        dim && "opacity-30",
      )}
    >
      <StatusDot status={card.status} />
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
      <span className="min-w-0 truncate font-medium text-[var(--vigil-text)]">{name}</span>
      {card.type && <span className="truncate text-[10px] text-[var(--vigil-muted)]">{card.type}</span>}
      {keyPath && <span className="text-[10px] text-amber-500">{t("topology.keyPathBadge")}</span>}
    </div>
  );
}

/** Port label: `port` first + dedup-merged `ports`, at most 3 joined with /, null when empty. */
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
 * Module-level state cache: route switches (topology→Overview→topology)
 * unmount/remount TopologyGraph and lose internal state (drag positions /
 * zoom / pan). Node positions and viewport are cached outside the component
 * and restored on remount — switching tabs back keeps the user's last graph
 * state. Note: cluster switch / force-layout recompute (in-component) still
 * goes through the layout-reset logic; the cache updates with nodes.
 */
let cachedNodePositions: Map<string, { x: number; y: number }> | null = null;
let cachedViewport: { x: number; y: number; zoom: number } | null = null;

export default function TopologyGraph({
  view,
  onSelect,
  onNodeFocus,
  focusedName,
  searchQuery,
  className,
}: {
  view: TopologyView;
  onSelect: (entity: GraphEntityRef) => void;
  /** List ↔ graph sync: fired when a node is clicked in the graph (TopologyPage scrolls the list). */
  onNodeFocus?: (name: string) => void;
  /** Node name highlighted in the graph from outside (list selection). */
  focusedName?: string | null;
  /** task27 B2: 搜索词 — 不命中的节点降透明度（filter/highlight only，不动布局）。 */
  searchQuery?: string;
  className?: string;
}) {
  const { t } = useTranslation();
  // Batch 49: cluster switch is a frontend filter (no /api/topology shape change).
  const options = useMemo(() => clusterOptions(view), [view]);
  const [cluster, setCluster] = useState<string>("all");
  // Force layout recomputes only when view/cluster changes (node selection is
  // a light data remap, no layout rerun — clicking to sync the list does not
  // jitter the canvas).
  const baseModel = useMemo(() => {
    const base = buildGraphModel(view);
    const filtered = filterGraphModel(base, cluster);
    return layoutForceGraph(filtered);
  }, [view, cluster]);
  const needle = (searchQuery ?? "").trim().toLowerCase();
  const model = useMemo(
    () => ({
      ...baseModel,
      nodes: baseModel.nodes.map((n) => ({
        ...n,
        data: {
          ...n.data,
          selected: focusedName != null && n.data.name === focusedName,
          dim: Boolean(needle) && !matchesSearch(n.data.card, needle),
        },
      })),
    }),
    [baseModel, focusedName, needle],
  );
  // Batch 49 fix: controlled mode requires onNodesChange, otherwise drags are
  // immediately overwritten by props (nodes feel undraggable). useNodesState
  // applies changes internally.
  // Initial value: restore cached positions on remount if present (keeps drag
  // state across tab switches), else force-layout initial positions.
  // useNodesState only takes the first value; afterwards driven by
  // onNodesChange / layout effects.
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
  // Drag/layout change → sync cache (module-level, survives unmount)
  useEffect(() => {
    cachedNodePositions = new Map(nodes.map((n) => [n.id, n.position]));
  }, [nodes]);
  // Cluster switch → force-layout recompute → reset layout. A view reference
  // change (tab-switch remount / refetch) does NOT trigger the reset — on
  // remount useNodesState already restored cached positions; recomputing the
  // force layout against the new view here would overwrite the cache (root
  // cause of positions resetting when switching back).
  // ⚠️ useEffect always runs once after mount: the first render must be
  // skipped, otherwise on remount this overwrites the positions useNodesState
  // restored from cache with the fresh layout (topology edits then switching
  // back still reset positions).
  const isFirstLayout = useRef(true);
  useEffect(() => {
    if (isFirstLayout.current) {
      isFirstLayout.current = false;
      return;
    }
    setNodes(baseModel.nodes.map((n) => ({ ...n, data: { ...n.data, selected: false, dim: false } })));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cluster, setNodes]);
  // focusedName change → update only the selected highlight, never position (keeps drags).
  useEffect(() => {
    setNodes((prev) =>
      prev.map((n) => {
        const fresh = model.nodes.find((m) => m.id === n.id);
        if (!fresh) return n;
        return { ...n, data: { ...n.data, selected: fresh.data.selected, dim: fresh.data.dim } };
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
  // Pan/zoom end → cache viewport (module-level, survives unmount)
  const handleMoveEnd = useCallback(
    (_e: unknown, viewport: { x: number; y: number; zoom: number }) => {
      cachedViewport = viewport;
    },
    [],
  );

  if (model.overflow) {
    return (
      <div className="flex h-[240px] items-center justify-center rounded-md border border-dashed border-[var(--vigil-border)] text-xs text-[var(--vigil-muted)]">
        {t("topology.overflow", { max: MAX_GRAPH_NODES })}
      </div>
    );
  }

  return (
    <div className={cn("flex h-[460px] w-full flex-col overflow-hidden rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)]", className)}>
      {options.length > 1 && (
        <div className="flex flex-wrap items-center gap-1 border-b border-[var(--vigil-border)] px-2.5 py-1.5">
          <span className="mr-1 text-[10px] text-[var(--vigil-muted)]">{t("topology.clusterLabel")}</span>
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
            {t("topology.allClusters")}
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
        // Cluster switch → node set change → remount triggers fitView (re-layout + fit).
        key={cluster}
        nodes={nodes}
        onNodesChange={onNodesChange}
        edges={model.edges}
        nodeTypes={nodeTypes}
        onNodeClick={handleNodeClick}
        onMoveEnd={handleMoveEnd}
        // On remount (tab switch back) restore the user's last zoom/pan; fitView only without cache.
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
