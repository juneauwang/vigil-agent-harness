// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import IncidentsPage from "./IncidentsPage";
import { api, ApiError } from "@/lib/api";
import type { IncidentItem, IncidentsResponse } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getIncidents: vi.fn(),
      markIncident: vi.fn(),
    },
  };
});

const ALERT_CPU: IncidentItem = {
  alertname: "HighCPU",
  severity: "critical",
  instance: "host-a:9090",
  startsAt: "2026-08-21T00:00:00Z",
  state: "active",
  collected_at: "2026-08-21T01:00:00Z",
  processed: false,
  source: "alertmanager",
};

function response(incidents: IncidentItem[]): IncidentsResponse {
  return {
    incidents,
    total: incidents.length,
    schema_version: 2,
    limit: 50,
    offset: 0,
    has_more: false,
  };
}

async function mountPage(resp: IncidentsResponse) {
  vi.mocked(api.getIncidents).mockResolvedValue(resp);
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter>
        <IncidentsPage />
      </MemoryRouter>,
    );
  });
  await act(async () => {});
  const cleanup = () => {
    root.unmount();
    container.remove();
  };
  return { container, cleanup };
}

async function renderPage(resp: IncidentsResponse) {
  const { container, cleanup } = await mountPage(resp);
  const text = container.textContent ?? "";
  cleanup();
  return text;
}

function buttonByText(container: HTMLElement, label: string): HTMLButtonElement {
  const found = Array.from(container.querySelectorAll("button")).find(
    (b) => (b.textContent ?? "").trim() === label,
  );
  if (!found) throw new Error(`button not found: ${label}`);
  return found as HTMLButtonElement;
}

async function click(el: HTMLElement) {
  await act(async () => {
    el.click();
    await Promise.resolve();
  });
}

describe("Incidents 告警页（批五十）", () => {
  beforeEach(() => {
    vi.mocked(api.getIncidents).mockReset();
    vi.mocked(api.markIncident).mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("渲染告警列表：字段 + severity 分级 + processed 标记", async () => {
    const text = await renderPage(
      response([
        ALERT_CPU,
        {
          ...ALERT_CPU,
          alertname: "DiskPressure",
          severity: "warning",
          instance: "host-b:9100",
          processed: true,
        },
        {
          ...ALERT_CPU,
          alertname: "NodeDown",
          severity: "info",
          instance: "host-c:9200",
        },
      ]),
    );
    expect(text).toContain("HighCPU");
    expect(text).toContain("host-a:9090");
    expect(text).toContain("2026-08-21T01:00:00Z");
    expect(text).toContain("alertmanager");
    expect(text).toContain("DiskPressure");
    expect(text).toContain("已处理");
    expect(text).toContain("NodeDown");
    expect(text).toContain("critical");
    expect(text).toContain("warning");
    expect(text).toContain("info");
    expect(text).toContain("3 条");
  });

  it("空 inbox → 暂无告警 空态", async () => {
    const text = await renderPage(response([]));
    expect(text).toContain("暂无告警");
    expect(text).not.toContain("条");
  });
});

describe("Incidents 人工处置（task33）", () => {
  beforeEach(() => {
    vi.mocked(api.getIncidents).mockReset();
    vi.mocked(api.markIncident).mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("点确认 → POST action=ack，行内显示已确认 + 取消确认", async () => {
    vi.mocked(api.markIncident).mockResolvedValue(
      response([{ ...ALERT_CPU, mark: "ack" }]),
    );
    const { container, cleanup } = await mountPage(response([ALERT_CPU]));

    await click(buttonByText(container, "确认"));

    expect(api.markIncident).toHaveBeenCalledWith({
      action: "ack",
      alertname: "HighCPU",
      instance: "host-a:9090",
      startsAt: "2026-08-21T00:00:00Z",
    });
    expect(container.textContent).toContain("已确认");
    expect(buttonByText(container, "取消确认")).toBeTruthy();
    cleanup();
  });

  it("取消确认 → POST action=unmark，行内回到未确认", async () => {
    vi.mocked(api.markIncident).mockResolvedValue(response([{ ...ALERT_CPU, mark: null }]));
    const { container, cleanup } = await mountPage(response([{ ...ALERT_CPU, mark: "ack" }]));

    await click(buttonByText(container, "取消确认"));

    expect(api.markIncident).toHaveBeenCalledWith({
      action: "unmark",
      alertname: "HighCPU",
      instance: "host-a:9090",
      startsAt: "2026-08-21T00:00:00Z",
    });
    expect(buttonByText(container, "确认")).toBeTruthy();
    cleanup();
  });

  it("清除需点两次：第一下变确认清除?（不请求），第二下才 POST action=clear 且行消失", async () => {
    vi.mocked(api.markIncident).mockResolvedValue(response([]));
    const { container, cleanup } = await mountPage(response([ALERT_CPU]));

    await click(buttonByText(container, "清除"));
    expect(api.markIncident).not.toHaveBeenCalled();
    expect(buttonByText(container, "确认清除?")).toBeTruthy();
    expect(container.textContent).toContain("HighCPU"); // 行仍在

    await click(buttonByText(container, "确认清除?"));
    expect(api.markIncident).toHaveBeenCalledWith({
      action: "clear",
      alertname: "HighCPU",
      instance: "host-a:9090",
      startsAt: "2026-08-21T00:00:00Z",
    });
    expect(container.textContent).not.toContain("HighCPU"); // 行消失
    cleanup();
  });

  it("第一下后 3 秒不点第二下 → 回落到清除", async () => {
    vi.useFakeTimers();
    const { container, cleanup } = await mountPage(response([ALERT_CPU]));

    await click(buttonByText(container, "清除"));
    expect(buttonByText(container, "确认清除?")).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(buttonByText(container, "清除")).toBeTruthy();
    expect(api.markIncident).not.toHaveBeenCalled();
    cleanup();
  });

  it("POST 失败（400）→ 显示错误，列表不变", async () => {
    vi.mocked(api.markIncident).mockRejectedValue(new ApiError("internal", "boom", 400));
    const { container, cleanup } = await mountPage(response([ALERT_CPU]));

    await click(buttonByText(container, "确认"));

    expect(container.textContent).toContain("boom");
    expect(container.textContent).toContain("HighCPU"); // 列表未变
    expect(buttonByText(container, "确认")).toBeTruthy(); // 未误标已确认
    expect(container.textContent).not.toContain("已确认");
    cleanup();
  });
});
