// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import OverviewPage from "./OverviewPage";
import { api } from "@/lib/api";
import type { TopologyView } from "@/lib/api";

// TopologyGraph（react-flow）在 jsdom 下需要 ResizeObserver 桩。
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
}

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getTopology: vi.fn(),
      getRunbooks: vi.fn(),
    },
  };
});

const VIEW_WITH_KEYPATH: TopologyView = {
  generated_at: "2026-08-18",
  data_root: "/tmp/vigil",
  version: 3,
  environments: ["prod"],
  clusters: [{ name: "k8s-prod", env: "prod" }],
  hosts: [
    {
      card: {
        name: "node1",
        env: "prod",
        cluster: "k8s-prod",
        status: "running",
        endpoint: "203.0.113.10",
      },
      services: [
        { card: { name: "gateway", env: "prod", status: "running" }, detail: null },
        { card: { name: "order-svc", env: "prod", status: "running" }, detail: null },
      ],
      services_missing: false,
    },
  ],
  cross_host: [],
  key_paths: [["gateway", "order-svc"]],
  key_path_entity_names: ["gateway", "order-svc"],
  details: {},
};

const VIEW_NO_KEYPATH: TopologyView = {
  ...VIEW_WITH_KEYPATH,
  key_paths: [],
  key_path_entity_names: [],
};

async function renderPage(view: TopologyView | null) {
  vi.mocked(api.getTopology).mockResolvedValue(
    view ? { ok: true, data: view, error: "" } : { ok: false, error: "boom", data: undefined },
  );
  vi.mocked(api.getRunbooks).mockResolvedValue({
    ok: true,
    error: "",
    data: { count: 0, runbooks: [] },
  });
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );
  });
  await act(async () => {});
  const text = container.textContent ?? "";
  root.unmount();
  container.remove();
  return text;
}

describe("批次四十五 Overview 整网连线拓扑（§BC/§BD）", () => {
  it("渲染整网拓扑 + 关键链路琥珀高亮（带 key_paths）", async () => {
    const text = await renderPage(VIEW_WITH_KEYPATH);
    expect(text).toContain("Topology Graph");
    expect(text).toContain("k8s-prod"); // cluster
    expect(text).toContain("node1"); // host
    expect(text).toContain("gateway"); // service
    expect(text).toContain("order-svc"); // service
    // 关键链路琥珀图例存在
    expect(text).toContain("关键链路");
  });

  it("key_paths 为空 → 仍显示完整层级拓扑 + 空态提示", async () => {
    const text = await renderPage(VIEW_NO_KEYPATH);
    expect(text).toContain("node1");
    expect(text).toContain("gateway");
    expect(text).toContain("未配置关键链路（key_paths）");
  });

  it("加载失败（无 view）→ 显示错误而非空白", async () => {
    const text = await renderPage(null);
    expect(text).toContain("boom");
  });

  it("图表例呈现（集群 / 主机 / 服务 / 关键链路）", async () => {
    const text = await renderPage(VIEW_WITH_KEYPATH);
    expect(text).toContain("集群");
    expect(text).toContain("主机");
    expect(text).toContain("服务");
  });
});
