// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import IncidentsPage from "./IncidentsPage";
import { api } from "@/lib/api";
import type { IncidentItem, IncidentsResponse } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getIncidents: vi.fn(),
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

async function renderPage(resp: IncidentsResponse) {
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
  const text = container.textContent ?? "";
  root.unmount();
  container.remove();
  return text;
}

describe("Incidents 告警页（批五十）", () => {
  beforeEach(() => {
    vi.mocked(api.getIncidents).mockReset();
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
