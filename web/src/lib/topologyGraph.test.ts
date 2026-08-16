import { describe, expect, it } from "vitest";

import type { TopologyHost, TopologyService, TopologyView } from "./api";
import { buildGraphModel, entityFromNode, nodeToneClass } from "./topologyGraph";

const svc = (name: string, status = "running"): TopologyService => ({
  card: { name, status, kind: "service" },
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
  key_paths: [["ingress", "gateway-svc", "order-db"]],
  key_path_entity_names: ["ingress", "gateway-svc", "order-db"],
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

  it("连线：cluster→host、host→service、cluster→cross、key_paths 高亮边", () => {
    const m = buildGraphModel(VIEW);
    const ids = new Set(m.edges.map((e) => e.id));
    expect(ids.has("cluster-host:prod:node1")).toBe(true);
    expect(ids.has("host-svc:node1:gateway-svc")).toBe(true);
    expect(ids.has("cluster-cross:prod:ingress")).toBe(true);
    // key_paths：ingress→gateway-svc→order-db 成边且标琥珀
    const kp = m.edges.filter((e) => e.className === "topo-edge-keypath");
    expect(kp.map((e) => e.id)).toEqual(["kp:ingress:gateway-svc", "kp:gateway-svc:order-db"]);
    expect(kp[0].style?.stroke).toBe("#f59e0b");
    // 链上节点标 keyPath
    const gw = m.nodes.find((n) => n.data.name === "gateway-svc");
    const ing = m.nodes.find((n) => n.data.name === "ingress");
    expect(gw?.data.keyPath).toBe(true);
    expect(ing?.data.keyPath).toBe(true);
    expect(m.nodes.find((n) => n.data.name === "kubelet")?.data.keyPath).toBe(false);
  });

  it("布局：集群按列排、服务在主机下方成网格不重叠", () => {
    const m = buildGraphModel(VIEW);
    const prod = m.nodes.find((n) => n.data.kind === "cluster" && n.data.name === "prod")!;
    const local = m.nodes.find((n) => n.data.kind === "cluster" && n.data.name === "local")!;
    expect(prod.position.x).toBe(0);
    expect(local.position.x).toBeGreaterThan(prod.position.x);
    // 同一 host 的多个服务 y 递增、x 分列
    const gw = m.nodes.find((n) => n.data.name === "gateway-svc")!;
    const db = m.nodes.find((n) => n.data.name === "order-db")!;
    expect(db.position.x).toBeGreaterThan(gw.position.x);
    expect(db.position.y).toBe(gw.position.y);
    // 节点位置无重叠：位置两两不相等
    const positions = m.nodes.map((n) => `${n.position.x},${n.position.y}`);
    expect(new Set(positions).size).toBe(positions.length);
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
