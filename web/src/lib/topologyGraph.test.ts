import { describe, expect, it } from "vitest";

import type { TopologyHost, TopologyService, TopologyView } from "./api";
import {
  buildGraphModel,
  clusterOptions,
  entityFromNode,
  filterGraphModel,
  layoutForceGraph,
  nodeToneClass,
} from "./topologyGraph";

const svc = (
  name: string,
  status = "running",
  dependsOn: string[] = [],
): TopologyService => ({
  card: { name, status, kind: "service", depends_on: dependsOn },
  detail: null,
});

const host = (name: string, cluster: string, services: TopologyService[]): TopologyHost => ({
  card: { name, cluster, kind: "host" },
  services,
  services_missing: services.length === 0,
  detail: null,
});

const VIEW: TopologyView = {
  generated_at: "2026-08-16 10:00:00",
  data_root: "~/.vigil",
  version: 3,
  sources: [],
  environments: [],
  clusters: [
    { name: "prod", env: "prod", status: "running", description: "生产" },
    { name: "local", env: "local", status: "", description: "" },
  ],
  hosts: [
    host("node1", "prod", [svc("gateway-svc"), svc("order-db", "stopped")]),
    host("node2", "prod", [svc("kubelet")]),
    host("laptop", "local", [svc("gitlab")]),
  ],
  cross_host: [
    { card: { name: "ingress", type: "ingress", cluster: "prod", status: "running", kind: "cross_host" }, detail: null },
  ],
  key_paths: [],
  key_path_entity_names: [],
  details: {},
};

describe("拓扑图数据模型（批三十五）", () => {
  it("三层节点映射：集群 → 主机 → 服务 + 跨主机实体", () => {
    const m = buildGraphModel(VIEW);
    const clusters = m.nodes.filter((n) => n.data.kind === "cluster");
    const hosts = m.nodes.filter((n) => n.data.kind === "host");
    const services = m.nodes.filter((n) => n.data.kind === "service");
    const crosses = m.nodes.filter((n) => n.data.kind === "cross_host");
    expect(clusters.map((n) => n.data.name)).toEqual(["prod", "local"]);
    expect(hosts.map((n) => n.data.name)).toEqual(["node1", "node2", "laptop"]);
    expect(services.map((n) => n.data.name)).toEqual(["gateway-svc", "order-db", "kubelet", "gitlab"]);
    expect(crosses.map((n) => n.data.name)).toEqual(["ingress"]);
    // 服务节点带宿主引用（抽屉/工具提示用）
    const gw = services.find((n) => n.data.name === "gateway-svc");
    expect(gw?.data.hostName).toBe("node1");
  });

  it("连线：cluster→host、host→service、cluster→cross、depends_on 高亮边", () => {
    // v0.4：key_paths 删除，services 层 depends_on 成琥珀依赖边。
    const VIEW2: TopologyView = {
      ...VIEW,
      hosts: [
        host("node1", "prod", [svc("gateway-svc", "running", ["order-db"]), svc("order-db", "stopped")]),
        host("node2", "prod", [svc("kubelet")]),
        host("laptop", "local", [svc("gitlab")]),
      ],
    };
    const m = buildGraphModel(VIEW2);
    const ids = new Set(m.edges.map((e) => e.id));
    expect(ids.has("cluster-host:prod:node1")).toBe(true);
    expect(ids.has("host-svc:node1:gateway-svc")).toBe(true);
    expect(ids.has("cluster-cross:prod:ingress")).toBe(true);
    // depends_on：gateway-svc → order-db 成边且标琥珀。
    const dep = m.edges.filter((e) => e.className === "topo-edge-keypath");
    expect(dep.map((e) => e.id)).toEqual(["dep:gateway-svc:order-db"]);
    expect(dep[0].style?.stroke).toBe("#f59e0b");
    // 依赖链上的节点标 keyPath（琥珀高亮），无关节点不标。
    expect(m.nodes.find((n) => n.data.name === "gateway-svc")?.data.keyPath).toBe(true);
    expect(m.nodes.find((n) => n.data.name === "order-db")?.data.keyPath).toBe(true);
    expect(m.nodes.find((n) => n.data.name === "kubelet")?.data.keyPath).toBe(false);
    // 悬空依赖（目标不存在）不画边。
    const VIEW3: TopologyView = { ...VIEW, hosts: [host("n1", "prod", [svc("a", "running", ["ghost"])])] };
    const m3 = buildGraphModel(VIEW3);
    expect(m3.edges.filter((e) => e.className === "topo-edge-keypath")).toHaveLength(0);
  });

  it("力导向布局：节点自由散布（非列排表格）、两两不重叠、结果确定", () => {
    const m = buildGraphModel(VIEW);
    const laid = layoutForceGraph(m);
    expect(laid.nodes).toHaveLength(m.nodes.length);
    // 布局后所有节点坐标有限且两两不重叠。
    const positions = laid.nodes.map((n) => `${n.position.x.toFixed(3)},${n.position.y.toFixed(3)}`);
    expect(new Set(positions).size).toBe(positions.length);
    for (const n of laid.nodes) {
      expect(Number.isFinite(n.position.x)).toBe(true);
      expect(Number.isFinite(n.position.y)).toBe(true);
    }
    // 集群节点不再同列排布：x/y 都有散布（力导向二维散布，非表格列排）。
    const clusters = laid.nodes.filter((n) => n.data.kind === "cluster");
    const xs = new Set(clusters.map((n) => n.position.x.toFixed(1)));
    const ys = new Set(clusters.map((n) => n.position.y.toFixed(1)));
    expect(xs.size).toBeGreaterThan(1);
    expect(ys.size).toBeGreaterThan(1);
    // 固定种子 → 两次布局结果一致（确定性，测试可复现）。
    const again = layoutForceGraph(m);
    expect(again.nodes.map((n) => `${n.position.x},${n.position.y}`)).toEqual(
      laid.nodes.map((n) => `${n.position.x},${n.position.y}`),
    );
  });

  it("力导向布局：空图/overflow 原样返回", () => {
    expect(layoutForceGraph({ nodes: [], edges: [], overflow: false })).toEqual({
      nodes: [],
      edges: [],
      overflow: false,
    });
    const big = buildGraphModel({
      ...VIEW,
      hosts: Array.from({ length: 400 }, (_, i) => host(`h${i}`, "prod", [svc("s")])),
      clusters: [{ name: "prod" }],
    });
    expect(big.overflow).toBe(true);
    expect(layoutForceGraph(big).nodes).toHaveLength(0);
  });

  it("集群筛选：只保留选中集群的节点与两端都在的连线", () => {
    const m = layoutForceGraph(buildGraphModel(VIEW));
    const local = filterGraphModel(m, "local");
    const names = local.nodes.map((n) => n.data.name);
    expect(names).toContain("local");
    expect(names).toContain("laptop");
    expect(names).toContain("gitlab");
    expect(names).not.toContain("prod");
    expect(names).not.toContain("node1");
    expect(names).not.toContain("gateway-svc");
    expect(names).not.toContain("ingress");
    for (const e of local.edges) {
      const src = local.nodes.some((n) => n.id === e.source);
      const dst = local.nodes.some((n) => n.id === e.target);
      expect(src && dst).toBe(true);
    }
    // "全部"原样返回（同一引用）。
    expect(filterGraphModel(m, "all")).toBe(m);
  });

  it("集群选项：显式集群优先，缺省归 default", () => {
    expect(clusterOptions(VIEW)).toEqual(["prod", "local"]);
    expect(clusterOptions({ ...VIEW, clusters: [] })).toEqual(["default"]);
    expect(clusterOptions({ ...VIEW, clusters: [], hosts: [], cross_host: [] })).toEqual([]);
  });

  it("节点数量上限保护（超过返回 overflow）", () => {
    const many = Array.from({ length: 400 }, (_, i) => host(`h${i}`, "prod", [svc("s")]));
    const big: TopologyView = { ...VIEW, hosts: many, clusters: [{ name: "prod" }] };
    const m = buildGraphModel(big);
    expect(m.overflow).toBe(true);
    expect(m.nodes).toHaveLength(0);
  });

  it("entityFromNode 还原抽屉实体引用（点击回调数据）", () => {
    const m = buildGraphModel(VIEW);
    const svcNode = m.nodes.find((n) => n.data.name === "order-db")!;
    const ref = entityFromNode(svcNode);
    expect(ref.kind).toBe("service");
    expect(ref.name).toBe("order-db");
    expect(ref.hostName).toBe("node1");
  });
});

describe("节点状态着色", () => {
  it("running 绿 / stopped 灰 / 关键链路琥珀优先", () => {
    expect(nodeToneClass("running")).toContain("emerald");
    expect(nodeToneClass("stopped")).toContain("vigil-card");
    expect(nodeToneClass("degraded")).toContain("amber");
    expect(nodeToneClass("running", true)).toContain("amber");
    expect(nodeToneClass(undefined)).toContain("vigil-card");
  });
});
