// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router";
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
      resetTopology: vi.fn(),
      getTopologyEntityRawYaml: vi.fn(),
      putTopologyEntityRawYaml: vi.fn(),
    },
  };
});

// CodeMirror 6 在 jsdom 下缺 DOM API——抽屉集成测试用 textarea 桩。
vi.mock("@uiw/react-codemirror", async () => {
  const { createElement } = await import("react");
  return {
    default: (props: { value: string; onChange: (v: string) => void }) =>
      createElement("textarea", {
        "aria-label": "yaml-stub",
        value: props.value,
        onChange: (e: { target: { value: string } }) => props.onChange(e.target.value),
      }),
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

// 批次四十五（§BC）：删除"拓扑总览"冗余卡片后，graph/card 模式主图仍完整渲染
// + 详情点跳正常（drawer 由卡片详情按钮承载；主图 onSelect 也喂同一 drawer state）。
describe("批次四十五 冗余总览卡片删除（§BC）", () => {
  it("不再渲染『拓扑总览（琥珀点 = 关键链路服务）』冗余卡", async () => {
    const text = await renderPage(VIEW);
    expect(text).not.toContain("拓扑总览（琥珀点 = 关键链路服务）");
  });

  it("graph/card 模式主图仍完整渲染三层拓扑 + 服务依赖量规", async () => {
    const text = await renderPage(VIEW);
    expect(text).toContain("node1");
    expect(text).toContain("prometheus");
    // v0.4：量规显示服务依赖连线数（取代 key_paths 关键链路）
    expect(text).toContain("条服务依赖连线");
  });
});

// 批八十五（OPS-DELTA #101）：清空拓扑入口（按钮 + 确认对话框 + reset API）。
describe("批八十五 清空拓扑入口", () => {
  async function mountPage() {
    vi.mocked(api.getTopology).mockResolvedValue({ ok: true, data: VIEW, error: "" });
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
    return { container, root };
  }

  it("确认对话框确认后调用 reset API 并显示未初始化提示", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const resetMock = vi.mocked(api.resetTopology).mockResolvedValue({
      ok: true,
      data: {
        ok: true,
        removed: ["entities", "services", "topology.yaml"],
        hosts: 1,
        clusters: 1,
        entities: 2,
      },
      error: "",
    });
    const { container, root } = await mountPage();
    try {
      const btn = [...container.querySelectorAll("button")].find(
        (b) => b.getAttribute("aria-label") === "清空拓扑",
      );
      expect(btn).toBeTruthy();
      await act(async () => {
        btn!.click();
      });
      expect(confirmSpy).toHaveBeenCalled();
      expect(resetMock).toHaveBeenCalledTimes(1);
      expect(container.textContent ?? "").toContain("拓扑已清空（未初始化）");
    } finally {
      root.unmount();
      container.remove();
      confirmSpy.mockRestore();
    }
  });

  it("取消确认则不调用 reset API", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    const resetMock = vi.mocked(api.resetTopology).mockClear();
    const { container, root } = await mountPage();
    try {
      const btn = [...container.querySelectorAll("button")].find(
        (b) => b.getAttribute("aria-label") === "清空拓扑",
      );
      await act(async () => {
        btn!.click();
      });
      expect(confirmSpy).toHaveBeenCalled();
      expect(resetMock).not.toHaveBeenCalled();
    } finally {
      root.unmount();
      container.remove();
      confirmSpy.mockRestore();
    }
  });
});

// ── task27 PART A: 实体 YAML 编辑入口（host/service/cluster/list 全覆盖）──
describe("TopologyPage raw YAML 编辑（task27 PART A）", () => {
  async function mountInteractive() {
    vi.clearAllMocks();
    vi.mocked(api.getTopology).mockResolvedValue({ ok: true, data: VIEW, error: "" });
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
    return { container, root };
  }

  it("卡片视图：host 卡 + service 行 + 集群标题都有 edit 按钮；点击 host 的打开抽屉加载原文", async () => {
    vi.mocked(api.getTopologyEntityRawYaml).mockResolvedValue("name: node1\n" as never);
    const { container, root } = await mountInteractive();
    try {
      // host(node1) + service(prometheus) + cluster heading(k8s-prod) = 3 个入口
      const editBtns = container.querySelectorAll<HTMLButtonElement>('[data-testid="topo-edit-yaml"]');
      expect(editBtns.length).toBe(3);

      // DOM 顺序：集群标题 edit → host 卡头 edit → service 行 edit
      const hostEdit = editBtns[1];
      await act(async () => {
        hostEdit.click();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(api.getTopologyEntityRawYaml).toHaveBeenCalledWith("host:node1");
      const drawer = container.querySelector('[data-testid="yaml-editor-drawer"]');
      expect(drawer).toBeTruthy();
      const textarea = container.querySelector("textarea");
      expect((textarea as HTMLTextAreaElement).value).toContain("name: node1");
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });

  it("service 行 edit 按钮用三段 entityId（service:<host>:<name>）", async () => {
    vi.mocked(api.getTopologyEntityRawYaml).mockResolvedValue("name: prometheus\n" as never);
    const { container, root } = await mountInteractive();
    try {
      const editBtns = container.querySelectorAll<HTMLButtonElement>('[data-testid="topo-edit-yaml"]');
      // editBtns[0] = 集群标题，[1] = host 卡头部，[2] = ServiceRow
      await act(async () => {
        editBtns[2].click();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(api.getTopologyEntityRawYaml).toHaveBeenCalledWith("service:node1:prometheus");
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });

  it("集群标题 edit 按钮 → cluster:<name>（topology.yaml 整文件）", async () => {
    vi.mocked(api.getTopologyEntityRawYaml).mockResolvedValue("clusters:\n" as never);
    const { container, root } = await mountInteractive();
    try {
      const editBtns = container.querySelectorAll<HTMLButtonElement>('[data-testid="topo-edit-yaml"]');
      await act(async () => {
        editBtns[0].click();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(api.getTopologyEntityRawYaml).toHaveBeenCalledWith("cluster:k8s-prod");
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });

  it("列表视图：每行（host/service）都有 edit 按钮", async () => {
    const { container, root } = await mountInteractive();
    try {
      const listBtn = [...container.querySelectorAll("button")].find(
        (b) => b.getAttribute("aria-label") === "列表视图",
      ) as HTMLButtonElement;
      expect(listBtn).toBeTruthy();
      await act(async () => {
        listBtn.click();
      });
      // host 行 + service 行 = 2 个（跨主机实体本 fixture 为空）
      const editBtns = container.querySelectorAll<HTMLButtonElement>('[data-testid="topo-edit-yaml"]');
      expect(editBtns.length).toBe(2);
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });

  it("保存成功 → putTopologyEntityRawYaml + 拓扑视图刷新", async () => {
    vi.mocked(api.getTopologyEntityRawYaml).mockResolvedValue("name: node1\n" as never);
    vi.mocked(api.putTopologyEntityRawYaml).mockResolvedValue({ ok: true, warnings: [] } as never);
    const { container, root } = await mountInteractive();
    try {
      const editBtns = container.querySelectorAll<HTMLButtonElement>('[data-testid="topo-edit-yaml"]');
      await act(async () => {
        editBtns[1].click();
        await Promise.resolve();
        await Promise.resolve();
      });
      const textarea = container.querySelector("textarea") as HTMLTextAreaElement;
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      await act(async () => {
        setter.call(textarea, "name: node1\nenv: prod\n");
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
      });
      const saveBtn = container.querySelector<HTMLButtonElement>('[data-testid="yaml-editor-save"]')!;
      await act(async () => {
        saveBtn.click();
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(api.putTopologyEntityRawYaml).toHaveBeenCalledWith("host:node1", "name: node1\nenv: prod\n");
      expect(api.getTopology).toHaveBeenCalledTimes(2); // 初次 + 保存后刷新
      expect(container.querySelector('[data-testid="yaml-editor-drawer"]')).toBeNull();
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });
});

// ── task27 PART B2: 搜索 → 图节点降透明度 + 命中统计/空结果提示 ──
describe("TopologyPage 搜索高亮（task27 PART B2）", () => {
  function typeSearch(container: HTMLElement, value: string) {
    const input = container.querySelector<HTMLInputElement>('[data-testid="topology-search"]')!;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  it("搜索不命中的图节点加 opacity-30（命中节点保持不透明）", async () => {
    vi.mocked(api.getTopology).mockResolvedValue({ ok: true, data: VIEW, error: "" });
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    try {
      await act(async () => {
        root.render(
          <MemoryRouter>
            <TopologyPage />
          </MemoryRouter>,
        );
      });
      await act(async () => {});
      // 无查询：图节点无降透明类（opacity-30 在自定义节点根 div，不在 rf 包装层）
      expect(container.querySelectorAll(".opacity-30").length).toBe(0);
      act(() => typeSearch(container, "prometheus"));
      await act(async () => {});
      const nodes = [...container.querySelectorAll(".react-flow__node")];
      expect(nodes.length).toBeGreaterThan(1);
      const dimmed = nodes.filter((n) => n.querySelector(".opacity-30"));
      const lit = nodes.filter((n) => !n.querySelector(".opacity-30"));
      // service(prometheus) 命中保持高亮；host/cluster 不命中变暗
      expect(dimmed.length).toBeGreaterThan(0);
      expect(lit.length).toBeGreaterThan(0);
      const litText = lit.map((n) => n.textContent ?? "").join("");
      expect(litText).toContain("prometheus");
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });

  it("命中数提示 + 空结果提示渲染", async () => {
    vi.mocked(api.getTopology).mockResolvedValue({ ok: true, data: VIEW, error: "" });
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    try {
      await act(async () => {
        root.render(
          <MemoryRouter>
            <TopologyPage />
          </MemoryRouter>,
        );
      });
      await act(async () => {});
      expect(container.querySelector('[data-testid="topo-search-hint"]')).toBeNull();

      act(() => typeSearch(container, "prometheus"));
      await act(async () => {});
      const hint = container.querySelector('[data-testid="topo-search-hint"]');
      expect(hint?.textContent).toContain("1 / 3");

      act(() => typeSearch(container, "no-such-entity"));
      await act(async () => {});
      expect(container.querySelector('[data-testid="topo-search-hint"]')?.textContent).toContain("no-such-entity");
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });
});

// ── task27 PART D: 顶栏搜索 → /topology?q= 过滤生效（断链修复）──
// 根因：页内 query 只在 useState 初始化器读一次 URL 参数——组件已挂载时
// 再次提交搜索（参数变化）不生效。修复 = 订阅 ?q= 显式同步进页内搜索框。
describe("TopologyPage 顶栏搜索断链修复（task27 PART D）", () => {
  function NavigateButton({ to, label }: { to: string; label: string }) {
    const navigate = useNavigate();
    return (
      <button type="button" data-testid={label} onClick={() => navigate(to)}>
        go
      </button>
    );
  }

  async function mountWithRouter(initial: string[]) {
    vi.mocked(api.getTopology).mockResolvedValue({ ok: true, data: VIEW, error: "" });
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={initial}>
          <NavigateButton to="/topology?q=prometheus" label="nav-search" />
          <Routes>
            <Route path="/topology" element={<TopologyPage />} />
          </Routes>
        </MemoryRouter>,
      );
    });
    await act(async () => {});
    return { container, root };
  }

  it("带 ?q= 首次进入：过滤已应用且页内搜索框反映查询", async () => {
    const { container, root } = await mountWithRouter(["/topology?q=prometheus"]);
    try {
      const input = container.querySelector<HTMLInputElement>('[data-testid="topology-search"]')!;
      expect(input.value).toBe("prometheus");
      // 命中提示渲染（1 host + 1 service + 1 cluster = total 3）
      expect(container.querySelector('[data-testid="topo-search-hint"]')?.textContent).toContain("1 / 3");
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });

  it("已挂载时再次提交搜索（?q 变化）→ 过滤实时更新（原断链场景）", async () => {
    const { container, root } = await mountWithRouter(["/topology", "/topology"]);
    try {
      // 初始无查询：无命中提示
      expect(container.querySelector('[data-testid="topo-search-hint"]')).toBeNull();
      // 模拟顶栏搜索提交：导航到 /topology?q=prometheus
      const nav = container.querySelector<HTMLButtonElement>('[data-testid="nav-search"]')!;
      await act(async () => {
        nav.click();
        await Promise.resolve();
      });
      const input = container.querySelector<HTMLInputElement>('[data-testid="topology-search"]')!;
      expect(input.value).toBe("prometheus");
      expect(container.querySelector('[data-testid="topo-search-hint"]')?.textContent).toContain("1 / 3");
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });

  it("无结果查询：空结果提示渲染（不再是『看似没反应』）", async () => {
    const { container, root } = await mountWithRouter(["/topology?q=nonsense-entity"]);
    try {
      const hint = container.querySelector('[data-testid="topo-search-hint"]');
      expect(hint?.textContent).toContain("nonsense-entity");
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });
});

// ── task28 P1.2: 实体卡左侧 4px 状态条（状态 token 着色）+ 列表行状态点 ──
describe("TopologyPage 状态条（task28 P1.2）", () => {
  it("故障实体的卡片左条用 error token，正常用 ok token；卡片有 hover 抬升类", async () => {
    const viewDown: TopologyView = {
      ...VIEW,
      hosts: [
        {
          card: { name: "node1", env: "prod", cluster: "k8s-prod", status: "down" },
          services: [],
          services_missing: true,
        },
        {
          card: { name: "node2", env: "prod", cluster: "k8s-prod", status: "running" },
          services: [],
          services_missing: true,
        },
      ],
    };
    vi.mocked(api.getTopology).mockResolvedValue({ ok: true, data: viewDown, error: "" });
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    try {
      await act(async () => {
        root.render(
          <MemoryRouter>
            <TopologyPage />
          </MemoryRouter>,
        );
      });
      await act(async () => {});
      const downCard = container.querySelector("#topo-row-node1") as HTMLElement;
      const okCard = container.querySelector("#topo-row-node2") as HTMLElement;
      expect(downCard.className).toContain("border-l-4");
      expect(downCard.className).toContain("border-l-[var(--vigil-error)]");
      expect(okCard.className).toContain("border-l-[var(--vigil-ok)]");
      expect(downCard.className).toContain("vigil-card-interactive");
      // 离线（无服务 host 卡为空状态推导）列表视图：行内有状态点
      const listBtn = [...container.querySelectorAll("button")].find(
        (b) => b.getAttribute("aria-label") === "列表视图",
      ) as HTMLButtonElement;
      await act(async () => {
        listBtn.click();
      });
      const row = container.querySelector("#topo-row-node1") as HTMLElement;
      expect(row.querySelector(".vigil-status-dot")).toBeTruthy();
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });
});
