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
});
