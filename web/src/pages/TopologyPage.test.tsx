// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import TopologyPage from "./TopologyPage";
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
    },
  };
});

const VIEW: TopologyView = {
  generated_at: "2026-08-16",
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
        ports: [22, 9090],
      },
      services: [
        {
          card: { name: "prometheus", env: "prod", status: "running", ports: [9090, 443] },
          detail: null,
        },
      ],
      services_missing: false,
    },
  ],
  cross_host: [],
  key_paths: [],
  key_path_entity_names: [],
  details: {},
};

async function renderPage(view: TopologyView) {
  vi.mocked(api.getTopology).mockResolvedValue({ ok: true, data: view, error: "" });
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter>
        <TopologyPage />
      </MemoryRouter>,
    );
  });
  await act(async () => {});
  const text = container.textContent ?? "";
  root.unmount();
  container.remove();
  return text;
}

describe("拓扑页 ports 字段展示（批三十七 §Y）", () => {
  it("主机与服务卡片渲染 ports 列表", async () => {
    const text = await renderPage(VIEW);
    expect(text).toContain("node1");
    expect(text).toContain("ports 22, 9090"); // Facts chip
    expect(text).toContain("prometheus");
    expect(text).toContain("ports 9090, 443"); // ServiceRow chip
  });

  it("无 ports 字段不渲染 ports 段", async () => {
    const text = await renderPage({
      ...VIEW,
      hosts: [
        {
          card: { name: "legacy", env: "prod", status: "running" },
          services: [],
          services_missing: true,
        },
      ],
    });
    expect(text).toContain("legacy");
    expect(text).not.toContain("ports ");
  });
});
