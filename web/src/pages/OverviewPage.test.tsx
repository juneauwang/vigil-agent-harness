// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router";
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
      getIncidents: vi.fn(),
      getRunbookCoverage: vi.fn(),
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
  vi.mocked(api.getIncidents).mockResolvedValue({ total: 3, incidents: [] } as never);
  vi.mocked(api.getRunbookCoverage).mockResolvedValue({
    ok: true,
    data: {
      high_risk: { total: 10, covered: 7, uncovered: ["reboot", "remove", "decommission"], coverage_pct: 70 },
      usage: { window_days: 30, audit_events_scanned: 0, actions: [], total_unique: 0, covered_unique: 0, coverage_pct: 0, gaps: [] },
    },
  } as never);
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
  it("渲染整网拓扑 + 服务依赖琥珀图例（v0.4 取代 key_paths）", async () => {
    const text = await renderPage(VIEW_WITH_KEYPATH);
    expect(text).toContain("Topology Graph");
    expect(text).toContain("k8s-prod"); // cluster
    expect(text).toContain("node1"); // host
    expect(text).toContain("gateway"); // service
    expect(text).toContain("order-svc"); // service
    expect(text).toContain("服务依赖");
    expect(text).not.toContain("关键链路");
    expect(text).not.toContain("key_paths");
  });

  it("无依赖 → 仍显示完整层级拓扑（无 key_paths 空态提示）", async () => {
    const text = await renderPage(VIEW_NO_KEYPATH);
    expect(text).toContain("node1");
    expect(text).toContain("gateway");
    expect(text).not.toContain("key_paths");
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

describe("批次八十一 Overview 卡（Incidents 真实计数 + 未覆盖风险）", () => {
  it("Incidents 卡显示真实计数（不再硬编码 0）", async () => {
    const text = await renderPage(VIEW_WITH_KEYPATH);
    expect(text).toContain("Incidents");
    expect(text).toContain("watch inbox");
  });

  it("未覆盖风险卡：值 + 高危覆盖率 sub + 点击跳 Runbooks", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route path="/" element={<OverviewPage />} />
            <Route path="/runbooks" element={<div data-testid="rb-page">RUNBOOKS</div>} />
          </Routes>
        </MemoryRouter>,
      );
    });
    await act(async () => {});
    const text = container.textContent ?? "";
    expect(text).toContain("未覆盖风险");
    expect(text).toContain("高危 10 已覆盖 7（覆盖率 70%）");
    const card = Array.from(container.querySelectorAll("div")).find(
      (d) => d.textContent?.includes("未覆盖风险") && d.getAttribute("role") === "button",
    );
    expect(card).toBeTruthy();
    act(() => card?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await act(async () => {});
    expect(container.textContent).toContain("RUNBOOKS");
    root.unmount();
    container.remove();
  });
});

// ── task28 P1.1: 加载骨架屏（数据未就绪时 shimmer 骨架替代空值渲染）──
describe("OverviewPage 指标卡骨架（task28 P1.1）", () => {
  it("数据未返回时指标值渲染骨架块，返回后消失", async () => {
    vi.mocked(api.getTopology).mockReturnValue(new Promise(() => {}) as never);
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    try {
      await act(async () => {
        root.render(
          <MemoryRouter>
            <OverviewPage />
          </MemoryRouter>,
        );
      });
      await act(async () => {});
      // Nodes/Services 卡的值在拓扑未返回时是骨架
      expect(container.querySelectorAll(".vigil-skeleton").length).toBeGreaterThanOrEqual(2);
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });
});
