// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { TopologyHost, TopologyService, TopologyView } from "@/lib/api";
import TopologyGraph from "./TopologyGraph";

// TopologyGraph（react-flow）在 jsdom 下需要 ResizeObserver 桩。
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
}

const svc = (name: string, port?: number | string): TopologyService => ({
  card: { name, status: "running", kind: "service", ...(port != null ? { port } : {}) },
  detail: null,
});
const host = (name: string, cluster: string, services: TopologyService[], ports?: Array<number | string>): TopologyHost => ({
  card: { name, cluster, kind: "host", ...(ports ? { ports } : {}) },
  services,
  services_missing: services.length === 0,
  detail: null,
});

const VIEW: TopologyView = {
  generated_at: "2026-08-16",
  data_root: "/tmp/vigil",
  version: 3,
  environments: ["prod", "local"],
  clusters: [
    { name: "prod", env: "prod", status: "running", description: "生产" },
    { name: "local", env: "local", status: "", description: "" },
  ],
  hosts: [
    host("node1", "prod", [svc("gateway-svc", 30443)], [22, 8080]),
    host("laptop", "local", [svc("gitlab")]),
  ],
  cross_host: [{ card: { name: "ingress", type: "ingress", cluster: "prod", status: "running", kind: "cross_host" }, detail: null }],
  key_paths: [["ingress", "gateway-svc"]],
  key_path_entity_names: ["ingress", "gateway-svc"],
  details: {},
};

async function renderGraph(opts?: { onSelect?: () => void; onNodeFocus?: (name: string) => void; focusedName?: string | null }) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <TopologyGraph
        view={VIEW}
        onSelect={opts?.onSelect ?? vi.fn()}
        onNodeFocus={opts?.onNodeFocus}
        focusedName={opts?.focusedName}
      />,
    );
  });
  await act(async () => {});
  return { container, root, text: () => container.textContent ?? "" };
}

describe("拓扑图力导向（批四十九）", () => {
  it("渲染集群标签栏（全部 + 各集群）+ 缩放控件 + 可拖节点", async () => {
    const { container, root, text } = await renderGraph();
    expect(text()).toContain("集群");
    expect(text()).toContain("全部");
    expect(text()).toContain("prod");
    expect(text()).toContain("local");
    // 缩放控件（Controls 在 react-flow 里渲染 zoom in/out/fit view 按钮）。
    const controls = container.querySelector(".react-flow__controls");
    expect(controls).toBeTruthy();
    const zoomIn = container.querySelector('button[aria-label="zoom in"]');
    expect(zoomIn).toBeTruthy();
    // 节点可拖：react-flow 默认 draggable（批三十五曾显式禁掉，此处恢复）。
    expect(container.querySelector(".react-flow__node")).toBeTruthy();
    root.unmount();
    container.remove();
  });

  it("集群切换：只显示选中集群的主机/服务，全部恢复", async () => {
    const { container, root, text } = await renderGraph();
    expect(text()).toContain("node1");
    expect(text()).toContain("gateway-svc");
    expect(text()).toContain("laptop");

    const localTab = Array.from(container.querySelectorAll("button")).find(
      (b) => b.getAttribute("data-testid") === "topo-cluster-local",
    )!;
    await act(async () => {
      localTab.click();
    });
    await act(async () => {});
    expect(text()).toContain("laptop");
    expect(text()).toContain("gitlab");
    expect(text()).not.toContain("node1");
    expect(text()).not.toContain("gateway-svc");
    expect(text()).not.toContain("ingress");

    const allTab = container.querySelector<HTMLButtonElement>('button[data-testid="topo-cluster-all"]')!;
    await act(async () => {
      allTab.click();
    });
    await act(async () => {});
    expect(text()).toContain("node1");
    expect(text()).toContain("laptop");

    root.unmount();
    container.remove();
  });

  it("点选节点回调 onSelect + onNodeFocus（列表联动数据源）", async () => {
    const onSelect = vi.fn();
    const onNodeFocus = vi.fn();
    const { container, root } = await renderGraph({ onSelect, onNodeFocus });
    // react-flow 节点标题含实体名，直接触发节点点击（.react-flow__node）。
    const nodeEl = Array.from(container.querySelectorAll<HTMLElement>(".react-flow__node")).find(
      (el) => el.textContent?.includes("node1"),
    )!;
    expect(nodeEl).toBeTruthy();
    await act(async () => {
      nodeEl.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await act(async () => {});
    expect(onSelect).toHaveBeenCalled();
    expect(onNodeFocus).toHaveBeenCalledWith("node1");
    root.unmount();
    container.remove();
  });

describe("端口显示", () => {
  it("服务/主机节点带端口时渲染端口标签", async () => {
    const { text } = await renderGraph();
    expect(text()).toContain(":30443");   // 服务端口
    expect(text()).toContain(":22/8080"); // 主机端口（去重合并）
    // 无端口服务（gitlab）不显示端口标签
    expect(text()).not.toMatch(/gitlab\s*:\d/);
  });
});

});
