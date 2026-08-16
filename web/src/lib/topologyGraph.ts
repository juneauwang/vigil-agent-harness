import type { Edge, Node } from "@xyflow/react";
import type { TopologyCard, TopologyView } from "./api";
import { statusTone } from "./ops";

/**
 * 拓扑图数据模型（批三十五）：GET /api/topology → react-flow nodes/edges。
 *
 * 纯函数（前端单测覆盖）：三层结构 集群 → 主机 → 服务 + 跨主机实体；
 * 手摆分层布局（数据天然三层，按集群分组列排，结构清晰不重叠、label 可见）；
 * 连线 host→services、cluster→host/cross_host、key_paths 链内相邻实体成
 * 琥珀高亮边；节点上限保护（防超大拓扑拖垮画布）。
 */
export const MAX_GRAPH_NODES = 300;

export const GRAPH_LAYOUT = {
  clusterW: 420,
  clusterTop: 52,
  hostW: 180,
  hostH: 46,
  svcW: 132,
  svcH: 38,
  svcCols: 3,
  blockGap: 26,
  crossH: 40,
} as const;

export type GraphNodeKind = "cluster" | "host" | "service" | "cross_host";

export interface GraphEntityRef {
  kind: GraphNodeKind;
  name: string;
  card: TopologyCard;
  detail?: Record<string, unknown> | null;
  hostName?: string;
}

export interface GraphNodeData extends GraphEntityRef {
  keyPath: boolean;
  // react-flow v12 Node<T> 要求 data 满足 Record<string, unknown>。
  [key: string]: unknown;
}

export type TopologyFlowNode = Node<GraphNodeData, GraphNodeKind>;

export interface TopologyGraphModel {
  nodes: TopologyFlowNode[];
  edges: Edge[];
  overflow: boolean;
}

const KIND_LABEL: Record<GraphNodeKind, string> = {
  cluster: "集群",
  host: "主机",
  service: "服务",
  cross_host: "跨主机实体",
};

export function kindLabel(kind: GraphNodeKind): string {
  return KIND_LABEL[kind] ?? kind;
}

/** 节点状态着色：关键链路琥珀优先；running 绿 / warn 琥珀 / error 红 / 其余灰。 */
export function nodeToneClass(status?: string, keyPath = false): string {
  if (keyPath) return "border-amber-500/60 bg-amber-500/10";
  switch (statusTone(status)) {
    case "ok":
      return "border-emerald-500/60 bg-emerald-500/10";
    case "warn":
      return "border-amber-500/60 bg-amber-500/10";
    case "error":
      return "border-red-500/60 bg-red-500/10";
    default:
      return "border-[var(--vigil-border)] bg-[var(--vigil-card)]";
  }
}

function _clusterCard(name: string, cluster: { env?: string; status?: string; description?: string } | undefined): TopologyCard {
  return { name, env: cluster?.env ?? "", status: cluster?.status ?? "", description: cluster?.description ?? "", kind: "cluster" };
}

/** 点击节点 → 抽屉实体引用（与卡片"详情"按钮同一形状）。 */
export function entityFromNode(node: TopologyFlowNode): GraphEntityRef {
  const { kind, name, card, detail, hostName } = node.data;
  return { kind, name, card, detail, hostName };
}

export function buildGraphModel(view: TopologyView): TopologyGraphModel {
  const kpNames = new Set(view.key_path_entity_names ?? []);
  const clusterMeta = new Map(view.clusters.map((c) => [c.name, c]));
  const clusterNames =
    view.clusters.length > 0
      ? view.clusters.map((c) => c.name)
      : view.hosts.length > 0 || view.cross_host.length > 0
        ? ["default"]
        : [];

  const nodes: TopologyFlowNode[] = [];
  const edges: Edge[] = [];
  let count = 0;

  const svcId = (host: string, name: string) => `service:${host}:${name}`;
  const hostId = (name: string) => `host:${name}`;
  const crossId = (name: string) => `cross:${name}`;

  clusterNames.forEach((cname, ci) => {
    const cx = ci * GRAPH_LAYOUT.clusterW;
    nodes.push({
      id: `cluster:${cname}`,
      type: "cluster",
      position: { x: cx, y: 0 },
      data: { kind: "cluster", name: cname, card: _clusterCard(cname, clusterMeta.get(cname)), keyPath: kpNames.has(cname) },
    });
    count += 1;

    let y = GRAPH_LAYOUT.clusterTop;
    for (const host of view.hosts) {
      if ((host.card.cluster || "default") !== cname) continue;
      const card = host.card;
      nodes.push({
        id: hostId(card.name),
        type: "host",
        position: { x: cx, y },
        data: { kind: "host", name: card.name, card, detail: host.detail, keyPath: kpNames.has(card.name) },
      });
      edges.push({
        id: `cluster-host:${cname}:${card.name}`,
        source: `cluster:${cname}`,
        target: hostId(card.name),
        type: "smoothstep",
      });
      count += 1;

      const rows = Math.max(1, Math.ceil(host.services.length / GRAPH_LAYOUT.svcCols));
      host.services.forEach((svc, si) => {
        const sc = svc.card;
        nodes.push({
          id: svcId(card.name, sc.name),
          type: "service",
          position: {
            x: cx + (si % GRAPH_LAYOUT.svcCols) * GRAPH_LAYOUT.svcW,
            y: y + GRAPH_LAYOUT.hostH + 18 + Math.floor(si / GRAPH_LAYOUT.svcCols) * GRAPH_LAYOUT.svcH,
          },
          data: { kind: "service", name: sc.name, card: sc, detail: svc.detail, hostName: card.name, keyPath: kpNames.has(sc.name) },
        });
        edges.push({
          id: `host-svc:${card.name}:${sc.name}`,
          source: hostId(card.name),
          target: svcId(card.name, sc.name),
          type: "smoothstep",
        });
        count += 1;
      });
      y += GRAPH_LAYOUT.hostH + 18 + rows * GRAPH_LAYOUT.svcH + GRAPH_LAYOUT.blockGap;
    }

    for (const ch of view.cross_host) {
      if ((ch.card.cluster || "default") !== cname) continue;
      const card = ch.card;
      nodes.push({
        id: crossId(card.name),
        type: "cross_host",
        position: { x: cx, y },
        data: { kind: "cross_host", name: card.name, card, detail: ch.detail, keyPath: kpNames.has(card.name) },
      });
      edges.push({
        id: `cluster-cross:${cname}:${card.name}`,
        source: `cluster:${cname}`,
        target: crossId(card.name),
        type: "smoothstep",
      });
      count += 1;
      y += GRAPH_LAYOUT.crossH;
    }
  });

  if (count > MAX_GRAPH_NODES) {
    return { nodes: [], edges: [], overflow: true };
  }

  // key_paths 链内相邻实体成高亮边（两端都存在于图中才画）。
  const byName = new Map<string, string>();
  for (const n of nodes) {
    if (n.data.kind !== "cluster" && !byName.has(n.data.name)) byName.set(n.data.name, n.id);
  }
  for (const chain of view.key_paths ?? []) {
    for (let i = 0; i + 1 < chain.length; i += 1) {
      const a = byName.get(chain[i]);
      const b = byName.get(chain[i + 1]);
      if (!a || !b || a === b) continue;
      edges.push({
        id: `kp:${chain[i]}:${chain[i + 1]}`,
        source: a,
        target: b,
        type: "smoothstep",
        className: "topo-edge-keypath",
        style: { stroke: "#f59e0b", strokeWidth: 2.5, opacity: 1 },
      });
    }
  }

  return { nodes, edges, overflow: false };
}
