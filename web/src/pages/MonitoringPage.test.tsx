// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import MonitoringPage from "./MonitoringPage";
import { ApiError, api } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getMonitoringHealth: vi.fn(),
      queryMonitoring: vi.fn(),
      getMonitoringAlertsTriage: vi.fn(),
      runRunbook: vi.fn(),
      getMonitoringConfig: vi.fn(),
      saveMonitoringConfig: vi.fn(),
      validateMonitoring: vi.fn(),
    },
  };
});

const HEALTH = {
  ok: true,
  data: {
    checked_at: "2026-08-23T12:00:00Z",
    cached: false,
    summary: { up: 1, down: 1, unknown: 1 },
    services: [
      {
        name: "web",
        host: "node1",
        cluster: "k8s-prod",
        type: "app",
        endpoint: "http://web.local:8080",
        status: "up",
        latency_ms: 1.5,
      },
      {
        name: "db",
        host: "node1",
        cluster: "k8s-prod",
        type: "db",
        endpoint: "node1:5432",
        status: "down",
        latency_ms: 2.0,
      },
      {
        name: "noep",
        host: "node1",
        cluster: "k8s-prod",
        type: "app",
        endpoint: null,
        status: "unknown",
        latency_ms: null,
      },
    ],
  },
};

const EMPTY_HEALTH = {
  ok: true,
  data: { checked_at: "2026-08-23T12:00:00Z", cached: false, summary: { up: 0, down: 0, unknown: 0 }, services: [] },
};

// internal（集群内部端口）行：不从外部探测，状态中性、不计入 up/down。
const INTERNAL_HEALTH = {
  ok: true,
  data: {
    checked_at: "2026-08-23T12:00:00Z",
    cached: false,
    summary: { up: 1, down: 1, unknown: 0, internal: 1 },
    services: [
      {
        name: "web",
        host: "node1",
        type: "app",
        endpoint: "http://web.local:8080",
        status: "up",
        latency_ms: 1.5,
      },
      {
        name: "argocd-redis",
        host: "node1",
        cluster: "k8s-prod",
        type: "cache",
        managed_by: "kubectl",
        endpoint: "node1:6379",
        status: "internal",
        latency_ms: null,
      },
      {
        name: "db",
        host: "node1",
        type: "db",
        endpoint: "node1:5432",
        status: "down",
        latency_ms: 3003.0,
      },
    ],
  },
};

const QUERY_RESULT = {
  ok: true,
  data: {
    kind: "range",
    query: "up",
    duration: "30m",
    step: "60s",
    series: [
      {
        name: "up",
        labels: { job: "node" },
        points: [
          [1724400000, 1],
          [1724400060, 0],
          [1724400120, 1],
        ],
        summary: { min: 0, max: 1, last: 1 },
        point_count: 3,
      },
    ],
    truncated: false,
  },
};

const ALERTS = {
  ok: true,
  data: {
    count: 1,
    matched_count: 0,
    unmatched_count: 1,
    alerts: [
      {
        alertname: "HighCPU",
        severity: "critical",
        instance: "node1:9100",
        startsAt: "2026-08-23T10:00:00Z",
        state: "active",
        disposition: { matched: false },
      },
    ],
  },
};

async function renderPage() {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter>
        <MonitoringPage />
      </MemoryRouter>,
    );
  });
  return { container, root };
}

async function submitQuery(container: HTMLElement, promql: string) {
  const input = container.querySelector("input") as HTMLInputElement;
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    "value",
  )!.set!;
  await act(async () => {
    setter.call(input, promql);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  const form = container.querySelector("form")!;
  await act(async () => {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("MonitoringPage", () => {
  it("renders health summary + three-state badges", async () => {
    vi.mocked(api.getMonitoringHealth).mockResolvedValue(HEALTH as never);
    vi.mocked(api.getMonitoringAlertsTriage).mockResolvedValue(ALERTS as never);
    const { container } = await renderPage();

    expect(api.getMonitoringHealth).toHaveBeenCalled();
    expect(container.textContent).toContain("服务健康");
    // 汇总 chips：up 1 / down 1 / unknown 1
    expect(container.textContent).toContain("up 1");
    expect(container.textContent).toContain("down 1");
    expect(container.textContent).toContain("unknown 1");
    // 三态徽标
    expect(container.querySelectorAll("span").length).toBeGreaterThan(0);
    const badges = Array.from(container.querySelectorAll("span"))
      .map((s) => s.textContent?.trim())
      .filter((t) => t === "up" || t === "down" || t === "unknown");
    expect(badges).toEqual(expect.arrayContaining(["up", "down", "unknown"]));
    expect(container.textContent).toContain("web");
    expect(container.textContent).toContain("db");
    expect(container.textContent).toContain("noep");
  });

  it("status filter narrows the table", async () => {
    vi.mocked(api.getMonitoringHealth).mockResolvedValue(HEALTH as never);
    vi.mocked(api.getMonitoringAlertsTriage).mockResolvedValue({ ok: true, data: { count: 0, matched_count: 0, unmatched_count: 0, alerts: [] } } as never);
    const { container } = await renderPage();

    expect(container.textContent).toContain("web");
    const downBtn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.includes("down 1"));
    expect(downBtn).toBeTruthy();
    await act(async () => {
      downBtn!.click();
    });
    expect(container.textContent).toContain("db");
    expect(container.textContent).not.toContain("web");
  });

  it("shows alerts with severity badge + empty state", async () => {
    vi.mocked(api.getMonitoringHealth).mockResolvedValue(HEALTH as never);
    vi.mocked(api.getMonitoringAlertsTriage).mockResolvedValue(ALERTS as never);
    const { container } = await renderPage();

    expect(container.textContent).toContain("活跃告警");
    expect(container.textContent).toContain("HighCPU");
    expect(container.textContent).toContain("critical");

    vi.mocked(api.getMonitoringAlertsTriage).mockResolvedValue({
      ok: true,
      data: { count: 0, matched_count: 0, unmatched_count: 0, alerts: [] },
    } as never);
    const { container: empty } = await renderPage();
    expect(empty.textContent).toContain("暂无活跃告警");
  });

  it("prometheus unconfigured shows guidance, health block still works", async () => {
    vi.mocked(api.getMonitoringHealth).mockResolvedValue(HEALTH as never);
    vi.mocked(api.getMonitoringAlertsTriage).mockResolvedValue(ALERTS as never);
    vi.mocked(api.queryMonitoring).mockRejectedValue(
      new ApiError(
        "prometheus_unavailable",
        "未配置 Prometheus（config ops.prometheus.endpoint），仅健康探测可用。",
        503,
      ),
    );
    const { container } = await renderPage();

    await submitQuery(container, "up");
    expect(container.textContent).toContain("Prometheus 未配置");
    expect(container.textContent).toContain("仅健康探测可用");
    // 健康块照常渲染
    expect(container.textContent).toContain("web");
  });

  it("query success renders series + sparkline", async () => {
    vi.mocked(api.getMonitoringHealth).mockResolvedValue(EMPTY_HEALTH as never);
    vi.mocked(api.getMonitoringAlertsTriage).mockResolvedValue({ ok: true, data: { count: 0, matched_count: 0, unmatched_count: 0, alerts: [] } } as never);
    vi.mocked(api.queryMonitoring).mockResolvedValue(QUERY_RESULT as never);
    const { container } = await renderPage();

    await submitQuery(container, "up");
    expect(container.textContent).toContain("1 个 series");
    expect(container.textContent).toContain("job=node");
    expect(container.querySelector('svg[aria-label="sparkline"]')).toBeTruthy();
  });

  it("health load failure shows error empty state", async () => {
    vi.mocked(api.getMonitoringHealth).mockRejectedValue(new ApiError("health_failed", "探测失败", 500));
    vi.mocked(api.getMonitoringAlertsTriage).mockResolvedValue({ ok: true, data: { count: 0, matched_count: 0, unmatched_count: 0, alerts: [] } } as never);
    const { container } = await renderPage();

    expect(container.textContent).toContain("健康数据加载失败");
    expect(container.textContent).toContain("探测失败");
  });

  // ── batch87（OPS-DELTA #103）：建议处置两态 + 执行走既有确认流 ─────────────

  it("shows disposition for matched and unmatched alerts", async () => {
    vi.mocked(api.getMonitoringHealth).mockResolvedValue(EMPTY_HEALTH as never);
    vi.mocked(api.getMonitoringAlertsTriage).mockResolvedValue({
      ok: true,
      data: {
        count: 2,
        matched_count: 1,
        unmatched_count: 1,
        alerts: [
          {
            alertname: "Harbor healthcheck failed",
            severity: "critical",
            startsAt: "2026-08-23T10:00:00Z",
            disposition: {
              matched: true,
              runbook: "harbor-restart",
              title: "Harbor 服务异常恢复",
              confidence: "high",
              matched_by: "trigger",
              matched_keyword: "harbor healthcheck failed",
              alternatives: [],
            },
          },
          {
            alertname: "DiskFull",
            severity: "warning",
            startsAt: "2026-08-23T09:00:00Z",
            disposition: { matched: false, hint: "无匹配 runbook" },
          },
        ],
      },
    } as never);
    const { container } = await renderPage();
    const text = container.textContent ?? "";
    expect(text).toContain("harbor-restart");
    expect(text).toContain("high");
    expect(text).toContain("触发词");
    expect(text).toContain("无匹配 runbook");
    expect(container.querySelector('a[href="/runbooks?name=harbor-restart"]')).toBeTruthy();
    expect(api.runRunbook).not.toHaveBeenCalled(); // l：渲染不自动执行
  });

  it("execute suggestion opens confirm then calls existing runbook API", async () => {
    vi.mocked(api.getMonitoringHealth).mockResolvedValue(EMPTY_HEALTH as never);
    vi.mocked(api.getMonitoringAlertsTriage).mockResolvedValue({
      ok: true,
      data: {
        count: 1,
        matched_count: 1,
        unmatched_count: 0,
        alerts: [
          {
            alertname: "Harbor healthcheck failed",
            severity: "critical",
            startsAt: "2026-08-23T10:00:00Z",
            disposition: {
              matched: true,
              runbook: "harbor-restart",
              confidence: "high",
              matched_by: "trigger",
              matched_keyword: "harbor healthcheck failed",
              alternatives: [],
            },
          },
        ],
      },
    } as never);
    vi.mocked(api.runRunbook).mockResolvedValue({
      ok: true,
      data: { exec_id: "exec-1" },
    } as never);
    const { container } = await renderPage();

    // 不点执行 → 零执行调用（l：建议不直通执行）。
    expect(api.runRunbook).not.toHaveBeenCalled();

    const alertSection = Array.from(container.querySelectorAll("section"))
      .find((s) => s.textContent?.includes("建议处置"));
    expect(alertSection).toBeTruthy();
    const execBtn = Array.from(alertSection!.querySelectorAll("button"))
      .find((b) => b.textContent?.includes("执行"));
    expect(execBtn).toBeTruthy();
    await act(async () => {
      execBtn!.click();
    });
    expect(container.textContent).toContain("执行 runbook");
    expect(container.textContent).toContain("确认执行");
    expect(api.runRunbook).not.toHaveBeenCalled(); // 确认前不执行

    const confirmBtn = Array.from(container.querySelectorAll("button"))
      .find((b) => b.textContent?.trim() === "确认执行");
    expect(confirmBtn).toBeTruthy();
    await act(async () => {
      confirmBtn!.click();
    });
    expect(api.runRunbook).toHaveBeenCalledWith(
      "harbor-restart",
      undefined,
      expect.objectContaining({ alertname: "Harbor healthcheck failed" }),
    );
    expect(container.textContent).toContain("已下发");
  });

  it("健康数据加载中显示「获取中…」提示", async () => {
    vi.mocked(api.getMonitoringHealth).mockReturnValue(
      new Promise(() => {}) as never, // never resolves → stays in loading
    );
    vi.mocked(api.getMonitoringAlertsTriage).mockResolvedValue(ALERTS as never);
    const { container } = await renderPage();
    expect(container.textContent).toContain("获取中");
  });

  // ── internal（集群内部端口）：中性徽标 + 过滤按钮，不计入 down ─────────────

  it("internal services render a neutral badge and filter, excluded from down", async () => {
    vi.mocked(api.getMonitoringHealth).mockResolvedValue(INTERNAL_HEALTH as never);
    vi.mocked(api.getMonitoringAlertsTriage).mockResolvedValue({ ok: true, data: { count: 0, matched_count: 0, unmatched_count: 0, alerts: [] } } as never);
    const { container } = await renderPage();

    // 汇总 chips：internal 1 独立计数（up 1 / down 1 不被 internal 撑大）。
    expect(container.textContent).toContain("internal 1");
    expect(container.textContent).toContain("up 1");
    expect(container.textContent).toContain("down 1");
    // internal 徽标存在（非绿/红中性 chip）。
    const badges = Array.from(container.querySelectorAll("span"))
      .map((s) => s.textContent?.trim())
      .filter((t) => t === "internal");
    expect(badges.length).toBe(1);
    // internal 过滤按钮：只显示 internal 行。
    const internalBtn = Array.from(container.querySelectorAll("button")).find(
      (b) => b.textContent?.includes("internal 1"),
    );
    expect(internalBtn).toBeTruthy();
    await act(async () => {
      internalBtn!.click();
    });
    expect(container.textContent).toContain("argocd-redis");
    expect(container.textContent).not.toContain("web");
    expect(container.textContent).not.toContain("db");
  });

  it("设置面板：打开读取当前配置，保存调用 API", async () => {
    vi.mocked(api.getMonitoringHealth).mockResolvedValue(HEALTH as never);
    vi.mocked(api.getMonitoringAlertsTriage).mockResolvedValue(ALERTS as never);
    vi.mocked(api.getMonitoringConfig).mockResolvedValue({
      ok: true,
      data: { endpoint: "http://old:9090", alertmanager: "" },
    } as never);
    vi.mocked(api.saveMonitoringConfig).mockResolvedValue({
      ok: true,
      data: { endpoint: "http://new:9090", alertmanager: "" },
    } as never);
    const { container } = await renderPage();
    const settingsBtn = Array.from(container.querySelectorAll("button")).find(
      (b) => b.textContent?.includes("设置"),
    )!;
    expect(settingsBtn).toBeTruthy();
    await act(async () => {
      settingsBtn.click();
    });
    expect(container.textContent).toContain("监控集成设置");
    // 打开时拉取当前值回填
    await act(async () => {
      await Promise.resolve();
    });
    const inputs = Array.from(container.querySelectorAll("input"));
    expect(inputs.some((i) => (i as HTMLInputElement).value === "http://old:9090")).toBe(true);
    // 改 endpoint 后保存
    const endpointInput = inputs.find((i) => (i as HTMLInputElement).value === "http://old:9090") as HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      "value",
    )!.set!;
    await act(async () => {
      setter.call(endpointInput, "http://new:9090");
      endpointInput.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const form = container.querySelector("form")!;
    await act(async () => {
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(api.saveMonitoringConfig).toHaveBeenCalledWith({
      endpoint: "http://new:9090",
      alertmanager: "",
    });
    expect(container.textContent).toContain("已保存");
  });
});

// ── task27 PART C: PromQL 输入行图标/文字不重叠 ──
// 根因：.vigil-input（非 layered 规则）恒胜 @layer utilities，输入上的 pl-8
// 被吞掉 → 左净空失效，绝对定位的放大镜图标与文字同框。修复 = 专用
// .vigil-input-icon（unlayered padding-left: 30px）。这里以类契约为断言
// （jsdom 不算 CSS）；像素级观感由维护者浏览器验收。
describe("MonitoringPage PromQL 输入图标净空（task27 PART C）", () => {
  it("PromQL 输入带 vigil-input-icon 净空类，不再依赖 pl-8", async () => {
    vi.mocked(api.getMonitoringHealth).mockResolvedValue(HEALTH as never);
    vi.mocked(api.getMonitoringAlertsTriage).mockResolvedValue(ALERTS as never);
    const { container } = await renderPage();
    const promqlInput = container.querySelector("input") as HTMLInputElement;
    expect(promqlInput.className).toContain("vigil-input");
    expect(promqlInput.className).toContain("vigil-input-icon");
    expect(promqlInput.className).not.toContain("pl-8");
    // 放大镜图标是输入框同容器（relative）内的兄弟绝对定位元素
    const wrapper = promqlInput.parentElement as HTMLElement;
    const icon = wrapper.querySelector("svg");
    expect(icon).toBeTruthy();
    const iconClass = typeof icon!.className === "string"
      ? icon!.className
      : icon!.className.baseVal;
    expect(iconClass).toContain("absolute");
  });

  it("同页其余输入（duration/step）无图标、不需要净空类", async () => {
    vi.mocked(api.getMonitoringHealth).mockResolvedValue(HEALTH as never);
    vi.mocked(api.getMonitoringAlertsTriage).mockResolvedValue(ALERTS as never);
    const { container } = await renderPage();
    const inputs = [...container.querySelectorAll("input")];
    expect(inputs.length).toBeGreaterThanOrEqual(3);
    for (const input of inputs.slice(1)) {
      expect(input.className).toContain("vigil-input");
      expect(input.className).not.toContain("vigil-input-icon");
    }
  });
});

// ── task31 PART A: 设置面板连接测试按钮 ──
describe("MonitoringPage 连接测试（task31 PART A）", () => {
  async function openSettings() {
    vi.mocked(api.getMonitoringHealth).mockResolvedValue(HEALTH as never);
    vi.mocked(api.getMonitoringAlertsTriage).mockResolvedValue(ALERTS as never);
    vi.mocked(api.getMonitoringConfig).mockResolvedValue({
      ok: true,
      data: { endpoint: "http://127.0.0.1:9090", alertmanager: "http://127.0.0.1:9093" },
    } as never);
    const { container } = await renderPage();
    const btn = [...container.querySelectorAll("button")].find(
      (b) => b.textContent?.includes("设置"),
    ) as HTMLButtonElement;
    await act(async () => {
      btn.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    return container;
  }

  function clickTest(container: HTMLElement) {
    const testBtn = container.querySelector<HTMLButtonElement>(
      '[data-testid="monitoring-test-connection"]',
    )!;
    expect(testBtn).toBeTruthy();
    act(() => testBtn.click());
  }

  it("测试按钮把当前表单值发给 /validate 并渲染逐目标 ✓ + 延迟", async () => {
    vi.mocked(api.validateMonitoring).mockResolvedValue({
      ok: true,
      results: {
        endpoint: { ok: true, latency_ms: 12.3, error: null },
        alertmanager: { ok: true, latency_ms: 8.1, error: null },
      },
    } as never);
    const container = await openSettings();
    clickTest(container);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(api.validateMonitoring).toHaveBeenCalledWith({
      endpoint: "http://127.0.0.1:9090",
      alertmanager: "http://127.0.0.1:9093",
    });
    const results = container.querySelector('[data-testid="monitoring-validate-results"]');
    expect(results).toBeTruthy();
    expect(results!.textContent).toContain("连通（12.3 ms）");
    expect(results!.textContent).toContain("连通（8.1 ms）");
    expect(results!.textContent).toContain("✓");
  });

  it("失败目标渲染 ✗ + 错误信息", async () => {
    vi.mocked(api.validateMonitoring).mockResolvedValue({
      ok: true,
      results: {
        endpoint: { ok: false, latency_ms: 5, error: "HTTP 401" },
      },
    } as never);
    const container = await openSettings();
    clickTest(container);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const results = container.querySelector('[data-testid="monitoring-validate-results"]');
    expect(results!.textContent).toContain("✗");
    expect(results!.textContent).toContain("HTTP 401");
    expect(results!.textContent).not.toContain("✓");
  });

  it("探测进行中按钮禁用（loading 态）", async () => {
    let resolveProbe: (v: unknown) => void = () => {};
    vi.mocked(api.validateMonitoring).mockReturnValue(
      new Promise((resolve) => { resolveProbe = resolve; }) as never,
    );
    const container = await openSettings();
    clickTest(container);
    await act(async () => {});
    const testBtn = container.querySelector<HTMLButtonElement>(
      '[data-testid="monitoring-test-connection"]',
    )!;
    expect(testBtn.disabled).toBe(true);
    expect(testBtn.textContent).toContain("探测中…");
    await act(async () => {
      resolveProbe({ ok: true, results: {} });
      await Promise.resolve();
      await Promise.resolve();
    });
    // 探测结束后恢复可用（草稿非空）
    expect(container.querySelector<HTMLButtonElement>(
      '[data-testid="monitoring-test-connection"]',
    )!.disabled).toBe(false);
  });
});
