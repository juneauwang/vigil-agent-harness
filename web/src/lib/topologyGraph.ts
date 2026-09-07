import type { Edge, Node } from "@xyflow/react";
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
} from "d3-force";
import type { TopologyCard, TopologyView } from "./api";
import { statusTone } from "./ops";
import i18n from "@/i18n";

/**
 * 拓扑图数据模型（批三十五）：GET /api/topology → react-flow nodes/edges。
 *
 * 纯函数（前端单测覆盖）：三层结构 集群 → 主机 → 服务 + 跨主机实体（v0.4
 * 恒空，兼容读取）；连线 host→services、cluster→host/cross_host、services
 * 层 depends_on 依赖成琥珀高亮边（v0.4 取代 key_paths 关键链路）；节点上限
 * 保护（防超大拓扑拖垮画布）。
 *
 * 批四十九：布局从"手摆分层列排"（视觉 = 表格/列表）改为 d3-force 力导向
 * （节点自由散布 + 关系连线，视觉 = 网络拓扑）。buildGraphModel 只产出节点/
 * 连线数据（初始坐标 = 确定性散布，无表格语义）；layoutForceGraph 用 d3-force
 * 同步跑固定 tick 数（种子固定 → 结果确定，前端单测可断言）。
 */
export const MAX_GRAPH_NODES = 300;

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
  /** 批四十九：列表中选中/图中点选的高亮标记（列表 ↔ 图联动）。 */
  selected?: boolean;
  /** task27 B2：搜索不命中 → 节点降透明度（filter/highlight，不改布局）。 */
  dim?: boolean;
  // react-flow v12 Node<T> 要求 data 满足 Record<string, unknown>。
  [key: string]: unknown;
}

export type TopologyFlowNode = Node<GraphNodeData, GraphNodeKind>;

export interface TopologyGraphModel {
  nodes: TopologyFlowNode[];
  edges: Edge[];
  overflow: boolean;
}

export interface ForceLayoutOptions {
  /** 画布逻辑宽度（fitView 会按容器缩放，这里只决定相对散布）。 */
  width?: number;
  height?: number;
  /** 同步推进的 tick 数（越大越收敛；300 节点上限内足够）。 */
  ticks?: number;
  /** 随机种子（默认固定 → 跨运行/跨测试确定）。 */
  seed?: number;
}

/** 力导向布局默认画布（相对散布基准，最终由 fitView 适配容器）。 */
const FORCE_LAYOUT_DEFAULTS = { width: 1400, height: 900, ticks: 240, seed: 20260820 } as const;

interface _SimNode {
  id: string;
  x: number;
  y: number;
}

interface _SimLink {
  source: string;
  target: string;
}

/** mulberry32 种子随机源：力导向结果跨运行确定（测试可复现）。 */
function _seededRandom(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s += 0x6d2b79f5;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** 初始散布：按索引均匀撒进画布 + 种子抖动（无任何表格/列排语义）。 */
function _initialPositions(
  nodes: TopologyFlowNode[],
  rand: () => number,
  width: number,
  height: number,
): Map<string, { x: number; y: number }> {
  const map = new Map<string, { x: number; y: number }>();
  const n = nodes.length;
  if (n === 0) return map;
  const cols = Math.max(1, Math.ceil(Math.sqrt(n * (width / height))));
  const rows = Math.max(1, Math.ceil(n / cols));
  nodes.forEach((node, i) => {
    const cx = (i % cols) / Math.max(1, cols - 1);
    const cy = Math.floor(i / cols) / Math.max(1, rows - 1);
    map.set(node.id, {
      x: (0.5 + 0.4 * (cx - 0.5)) * width + (rand() - 0.5) * width * 0.12,
      y: (0.5 + 0.4 * (cy - 0.5)) * height + (rand() - 0.5) * height * 0.12,
    });
  });
  return map;
}

/**
 * d3-force 力导向布局（批四十九核心）：同步跑固定 tick 数后把坐标写回节点。
 *
 * 力模型：cluster→host→service 树形 link（距离 120）+ 全局斥力 + 按节点
 * 类别半径的碰撞避免 + 画布中心。节点自由散布 + 连线表达关系——视觉上是
 * 网络拓扑而非表格。返回新 model（不改入参），种子固定保证结果确定。
 */
export function layoutForceGraph(
  model: TopologyGraphModel,
  options: ForceLayoutOptions = {},
): TopologyGraphModel {
  const width = options.width ?? FORCE_LAYOUT_DEFAULTS.width;
  const height = options.height ?? FORCE_LAYOUT_DEFAULTS.height;
  const ticks = options.ticks ?? FORCE_LAYOUT_DEFAULTS.ticks;
  const seed = options.seed ?? FORCE_LAYOUT_DEFAULTS.seed;
  if (model.overflow || model.nodes.length === 0) return model;

  const rand = _seededRandom(seed);
  const init = _initialPositions(model.nodes, rand, width, height);
  const simNodes: _SimNode[] = model.nodes.map((n) => ({
    id: n.id,
    x: init.get(n.id)?.x ?? 0,
    y: init.get(n.id)?.y ?? 0,
  }));
  const simLinks: _SimLink[] = model.edges.map((e) => ({
    source: String(e.source),
    target: String(e.target),
  }));

  // 碰撞半径按类别：集群节点宽（≈400px），主机/服务小一些——防集群堆叠。
  const kindById = new Map(model.nodes.map((n) => [n.id, n.data.kind]));
  const collideRadius = (d: _SimNode): number => {
    const kind = kindById.get(d.id);
    if (kind === "cluster") return 130;
    if (kind === "host") return 88;
    if (kind === "cross_host") return 80;
    return 58;
  };

  const sim = forceSimulation<_SimNode, _SimLink>(simNodes)
    .force(
      "link",
      forceLink<_SimNode, _SimLink>(simLinks)
        .id((d) => d.id)
        .distance(120)
        .strength(0.3),
    )
    .force("charge", forceManyBody<_SimNode>().strength(-220))
    .force("collide", forceCollide<_SimNode>().radius(collideRadius).strength(0.7))
    .force("center", forceCenter(width / 2, height / 2))
    .stop();
  sim.randomSource(rand);
  for (let i = 0; i < ticks; i += 1) sim.tick();

  const posById = new Map(simNodes.map((d) => [d.id, { x: d.x, y: d.y }]));
  return {
    ...model,
    nodes: model.nodes.map((n) => {
      const p = posById.get(n.id);
      return p ? { ...n, position: { x: p.x, y: p.y } } : n;
    }),
  };
}

/** 集群筛选选项：与 buildGraphModel 的集群分组同源（"全部"由调用方渲染）。 */
export function clusterOptions(view: TopologyView): string[] {
  if (view.clusters.length > 0) return view.clusters.map((c) => c.name);
  if (view.hosts.length > 0 || view.cross_host.length > 0) return ["default"];
  return [];
}

/** 按集群过滤图模型（"全部"/空 → 原样返回；节点/连线两端都在才保留）。 */
export function filterGraphModel(
  model: TopologyGraphModel,
  cluster: string,
): TopologyGraphModel {
  if (!cluster || cluster === "all") return model;
  const hostByCluster = new Map<string, string>();
  for (const n of model.nodes) {
    if (n.data.kind === "host") {
      hostByCluster.set(n.data.name, n.data.card.cluster || "default");
    }
  }
  const keep = new Set<string>();
  for (const n of model.nodes) {
    const d = n.data;
    if (d.kind === "cluster") {
      if (d.name === cluster) keep.add(n.id);
    } else if (d.kind === "host") {
      if ((d.card.cluster || "default") === cluster) keep.add(n.id);
    } else if (d.kind === "cross_host") {
      if ((d.card.cluster || "default") === cluster) keep.add(n.id);
    } else if (d.kind === "service") {
      const hostCluster = d.hostName ? hostByCluster.get(d.hostName) : undefined;
      if (hostCluster === cluster) keep.add(n.id);
    }
  }
  const nodes = model.nodes.filter((n) => keep.has(n.id));
  const ids = new Set(nodes.map((n) => n.id));
  const edges = model.edges.filter(
    (e) => ids.has(String(e.source)) && ids.has(String(e.target)),
  );
  return { ...model, nodes, edges };
}

const KIND_KEY: Record<GraphNodeKind, string> = {
  cluster: "topology.kind.cluster",
  host: "topology.kind.host",
  service: "topology.kind.service",
  cross_host: "topology.kind.cross_host",
};

export function kindLabel(kind: GraphNodeKind): string {
  return i18n.t(KIND_KEY[kind] ?? kind);
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
    nodes.push({
      id: `cluster:${cname}`,
      type: "cluster",
      // 初始坐标：确定性散布（无表格语义），真实布局由 layoutForceGraph 计算。
      position: { x: (ci % 7) * 180 + 60, y: Math.floor(ci / 7) * 200 + 40 },
      data: { kind: "cluster", name: cname, card: _clusterCard(cname, clusterMeta.get(cname)), keyPath: kpNames.has(cname) },
    });
    count += 1;

    for (const host of view.hosts) {
      if ((host.card.cluster || "default") !== cname) continue;
      const card = host.card;
      nodes.push({
        id: hostId(card.name),
        type: "host",
        position: { x: ci * 180 + 40, y: 0 },
        data: { kind: "host", name: card.name, card, detail: host.detail, keyPath: kpNames.has(card.name) },
      });
      edges.push({
        id: `cluster-host:${cname}:${card.name}`,
        source: `cluster:${cname}`,
        target: hostId(card.name),
        type: "default",
      });
      count += 1;

      host.services.forEach((svc, _si) => {
        const sc = svc.card;
        nodes.push({
          id: svcId(card.name, sc.name),
          type: "service",
          position: { x: ci * 180 + 60, y: 20 },
          data: { kind: "service", name: sc.name, card: sc, detail: svc.detail, hostName: card.name, keyPath: kpNames.has(sc.name) },
        });
        edges.push({
          id: `host-svc:${card.name}:${sc.name}`,
          source: hostId(card.name),
          target: svcId(card.name, sc.name),
          type: "default",
        });
        count += 1;
      });
    }

    for (const ch of view.cross_host) {
      if ((ch.card.cluster || "default") !== cname) continue;
      const card = ch.card;
      nodes.push({
        id: crossId(card.name),
        type: "cross_host",
        position: { x: ci * 180 + 80, y: 40 },
        data: { kind: "cross_host", name: card.name, card, detail: ch.detail, keyPath: kpNames.has(card.name) },
      });
      edges.push({
        id: `cluster-cross:${cname}:${card.name}`,
        source: `cluster:${cname}`,
        target: crossId(card.name),
        type: "default",
      });
      count += 1;
    }
  });

  if (count > MAX_GRAPH_NODES) {
    return { nodes: [], edges: [], overflow: true };
  }

  // v0.4：services 层 depends_on 依赖成琥珀高亮边（取代 key_paths 关键链路；
  // 两端都存在于图中才画，跨主机/缺失目标自动跳过）。依赖链上的节点标 keyPath。
  const byName = new Map<string, string>();
  for (const n of nodes) {
    if (n.data.kind !== "cluster" && !byName.has(n.data.name)) byName.set(n.data.name, n.id);
  }
  const onDep = new Set<string>();
  for (const n of nodes) {
    if (n.data.kind !== "service") continue;
    for (const dep of n.data.card.depends_on ?? []) {
      const target = byName.get(dep);
      if (!target || target === n.id) continue;
      onDep.add(n.id);
      onDep.add(target);
      edges.push({
        id: `dep:${n.data.name}:${dep}`,
        source: n.id,
        target,
        type: "default",
        className: "topo-edge-keypath",
        style: { stroke: "#f59e0b", strokeWidth: 2.5, opacity: 1 },
      });
    }
  }
  if (onDep.size > 0) {
    for (const n of nodes) {
      if (onDep.has(n.id)) n.data.keyPath = true;
    }
  }

  return { nodes, edges, overflow: false };
}
