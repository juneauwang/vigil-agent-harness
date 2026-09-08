// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import App from "./App";
import { api } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getHealth: vi.fn(),
      getTopology: vi.fn(),
      getStatus: vi.fn(),
      getApprovals: vi.fn(),
      getIncidents: vi.fn(),
      getRunbooks: vi.fn(),
      getRunbookCoverage: vi.fn(),
    },
  };
});

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  vi.mocked(api.getHealth).mockResolvedValue({
    ok: true,
    version: "1.0.3",
    auth_required: false,
    uptime_seconds: 10,
  } as never);
  vi.mocked(api.getTopology).mockResolvedValue({ ok: false, error: "" } as never);
  vi.mocked(api.getStatus).mockResolvedValue({ version: "1.0.3" } as never);
  vi.mocked(api.getApprovals).mockResolvedValue({
    ok: true,
    approvals: [],
    total: 0,
  } as never);
  vi.mocked(api.getIncidents).mockResolvedValue({ total: 0, incidents: [] } as never);
  vi.mocked(api.getRunbooks).mockResolvedValue({
    ok: true,
    data: { count: 0, runbooks: [] },
  } as never);
  vi.mocked(api.getRunbookCoverage).mockResolvedValue({
    ok: true,
    data: {
      high_risk: { total: 0, covered: 0, uncovered: [], coverage_pct: 0 },
      usage: { window_days: 30, audit_events_scanned: 0, actions: [], total_unique: 0, covered_unique: 0, coverage_pct: 0, gaps: [] },
    },
  } as never);
});

describe("App 侧栏（task28 P0.2）", () => {
  async function mountAt(route: string) {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={[route]}>
          <App />
        </MemoryRouter>,
      );
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    return { container, root };
  }

  it("当前路由的侧栏项有主色左侧条 + 渐变底；非激活项为 muted", async () => {
    const { container, root } = await mountAt("/runbooks");
    try {
      const active = [...container.querySelectorAll("aside a")].find(
        (a) => a.getAttribute("href") === "/runbooks",
      );
      expect(active).toBeTruthy();
      expect(active!.className).toContain("shadow-[inset_3px_0_0_var(--vigil-primary)]");
      expect(active!.className).toContain("text-[var(--vigil-primary)]");
      const inactive = [...container.querySelectorAll("aside a")].find(
        (a) => a.getAttribute("href") === "/audit",
      );
      expect(inactive!.className).toContain("text-[var(--vigil-muted)]");
      expect(inactive!.className).not.toContain("shadow-[inset_3px_0_0");
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });

  it("侧栏页脚渲染版本行（读自 /api/health version，非 i18n 硬编码）", async () => {
    const { container, root } = await mountAt("/overview");
    try {
      const version = container.querySelector('[data-testid="sidebar-version"]');
      expect(version).toBeTruthy();
      expect(version!.textContent).toBe("v1.0.3");
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });
});
