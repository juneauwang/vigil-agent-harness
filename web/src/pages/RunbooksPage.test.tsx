// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import RunbooksPage from "./RunbooksPage";
import { api } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getRunbooks: vi.fn(),
      getRunbook: vi.fn(),
      getRunbookExecutions: vi.fn().mockResolvedValue({
        ok: true,
        data: { count: 0, executions: [] },
      } as never),
      runRunbook: vi.fn(),
      runbookProgressStream: vi.fn(),
      getRunbookCoverage: vi.fn(),
    },
  };
});

const V2_RUNBOOK: Record<string, unknown> = {
  name: "nginx-config-update",
  title: "Nginx 配置变更并生效",
  version: 2,
  kind: "maintenance",
  env: "prod",
  clusters: ["k3s-prod"],
  host_groups: [],
  hosts: [],
  schedule: { cron: "0 2 * * 3", timezone: "Asia/Shanghai" },
  on_failure: { rollback: "rollback-main" },
  triggers: [
    "harbor healthcheck failed",
    { alertname: "HarborHealthcheckDown", severity: "critical" },
  ],
  rollback: [
    {
      name: "rollback-main",
      steps: [
        { id: "rb-restore", title: "恢复配置", action: "restore", params: { target: "nginx" } },
      ],
    },
  ],
  steps: [
    {
      id: "backup",
      title: "变更前备份",
      action: "backup",
      params: { target: "nginx", dest: "/backup/nginx/config/latest" },
      on_failure: "stop",
    },
    {
      id: "apply",
      title: "应用配置变更",
      action: "apply_config",
      params: {
        target: "nginx",
        changes: [{ key: "http.server_tokens", value: "off" }],
      },
      on_failure: { rollback: "rollback-main" },
    },
    {
      id: "verify",
      title: "验证生效",
      action: "verify",
      params: { target: "nginx" },
      expect: { target: "http", url: "http://127.0.0.1/healthz", http_status: 200 },
    },
  ],
};

const V1_RUNBOOK: Record<string, unknown> = {
  name: "harbor-restart",
  title: "Harbor 服务异常恢复",
  version: 1,
  kind: "incident",
  env: "prod",
  triggers: ["harbor healthcheck failed"],
  steps: [{ id: "restart", title: "重启", commands: ["docker restart harbor"] }],
};

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  vi.mocked(api.getRunbookCoverage).mockResolvedValue({
    ok: true,
    data: {
      high_risk: { total: 0, covered: 0, uncovered: [], coverage_pct: 0 },
      usage: { window_days: 30, audit_events_scanned: 0, actions: [], total_unique: 0, covered_unique: 0, coverage_pct: 0, gaps: [] },
    },
  } as never);
});

function render(page: React.ReactElement) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => root.render(<MemoryRouter>{page}</MemoryRouter>));
  return container;
}

describe("RunbooksPage", () => {
  it("renders v0.2 runbook fields（action/params/expect/schedule/场景回滚/双形态 triggers）", async () => {
    vi.mocked(api.getRunbooks).mockResolvedValue({
      ok: true,
      data: { count: 1, runbooks: [{ name: "nginx-config-update", title: "Nginx 配置变更并生效", step_count: 3 }] },
    } as never);
    vi.mocked(api.getRunbook).mockResolvedValue({ ok: true, data: V2_RUNBOOK } as never);

    const container = render(<RunbooksPage />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const text = container.textContent ?? "";
    expect(text).toContain("schema: v2（声明式动作）");
    expect(text).toContain("backup");
    expect(text).toContain("apply_config");
    expect(text).toContain("verify");
    expect(text).toContain("target:nginx");
    expect(text).toContain("http.server_tokens");
    expect(text).toContain("http_status: 200");
    expect(text).toContain("schedule:");
    expect(text).toContain("0 2 * * 3");
    expect(text).toContain("Asia/Shanghai");
    expect(text).toContain("rollback-main");
    expect(text).toContain("HarborHealthcheckDown");
    expect(text).toContain("on_failure: 回滚 → rollback-main");
    expect(text).toContain("目标范围: 集群 k3s-prod");
  });

  it("keeps v0.1 commands rendering unchanged", async () => {
    vi.mocked(api.getRunbooks).mockResolvedValue({
      ok: true,
      data: { count: 1, runbooks: [{ name: "harbor-restart", title: "Harbor 服务异常恢复", step_count: 1 }] },
    } as never);
    vi.mocked(api.getRunbook).mockResolvedValue({ ok: true, data: V1_RUNBOOK } as never);

    const container = render(<RunbooksPage />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const text = container.textContent ?? "";
    expect(text).toContain("docker restart harbor");
    expect(text).toContain("schema: v1");
  });

  it("shows v0.2 runbook with 执行 button and streams live progress（确认 → 实时步骤 → 终态）", async () => {
    vi.mocked(api.getRunbooks).mockResolvedValue({
      ok: true,
      data: { count: 1, runbooks: [{ name: "nginx-config-update", title: "Nginx 配置变更并生效", step_count: 3 }] },
    } as never);
    vi.mocked(api.getRunbook).mockResolvedValue({ ok: true, data: V2_RUNBOOK } as never);
    vi.mocked(api.runRunbook).mockResolvedValue({
      ok: true,
      data: { exec_id: "exec_run1", runbook: "nginx-config-update", env: "prod", status: "running" },
    } as never);
    vi.mocked(api.runbookProgressStream).mockImplementation(async (_execId: string, onEvent) => {
      onEvent({ type: "step_start", step_id: "backup", title: "变更前备份", action: "backup", target: "nginx", status: "running" });
      onEvent({ type: "step_done", step_id: "backup", title: "变更前备份", action: "backup", target: "nginx", status: "ok" });
      onEvent({ type: "step_start", step_id: "apply", title: "应用配置变更", action: "apply_config", target: "nginx", status: "running" });
      onEvent({ type: "step_done", step_id: "apply", title: "应用配置变更", action: "apply_config", target: "nginx", status: "ok" });
      onEvent({ type: "runbook_done", status: "ok", duration_s: 1.24, step_count: 2 });
    });

    const container = render(<RunbooksPage />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const runBtn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.includes("执行"));
    expect(runBtn).toBeTruthy();
    act(() => runBtn?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    const text0 = container.textContent ?? "";
    expect(text0).toContain("确认执行 nginx-config-update");

    const confirmBtn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "确认执行");
    act(() => confirmBtn?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(api.runRunbook).toHaveBeenCalledWith("nginx-config-update");
    expect(api.runbookProgressStream).toHaveBeenCalledWith("exec_run1", expect.any(Function), expect.anything());
    const text = container.textContent ?? "";
    expect(text).toContain("执行进度");
    expect(text).toContain("变更前备份");
    expect(text).toContain("应用配置变更");
    expect(text).toContain("执行完成");
    expect(text).toContain("1.2s");
  });

  it("renders running execution row（运行中徽标 + 展开实时步骤）", async () => {
    vi.mocked(api.getRunbooks).mockResolvedValue({
      ok: true,
      data: { count: 1, runbooks: [{ name: "nginx-config-update", title: "Nginx 配置变更并生效", step_count: 3 }] },
    } as never);
    vi.mocked(api.getRunbook).mockResolvedValue({ ok: true, data: V2_RUNBOOK } as never);
    vi.mocked(api.getRunbookExecutions).mockResolvedValue({
      ok: true,
      data: {
        count: 1,
        executions: [],
        running: [
          { exec_id: "exec_live1", runbook: "nginx-config-update", env: "prod", started_at: "2026-08-23T10:00:00+08:00" },
        ],
      },
    } as never);
    vi.mocked(api.runbookProgressStream).mockImplementation(async (_execId: string, onEvent) => {
      onEvent({ type: "step_start", step_id: "backup", title: "变更前备份", action: "backup", status: "running" });
    });

    const container = render(<RunbooksPage />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const text = container.textContent ?? "";
    expect(text).toContain("运行中");
    expect(text).toContain("锁定中");

    const liveRow = Array.from(container.querySelectorAll("tr")).find(
      (tr) => tr.textContent?.includes("nginx-config-update") && tr.textContent?.includes("运行中"),
    );
    act(() => liveRow?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(api.runbookProgressStream).toHaveBeenCalledWith("exec_live1", expect.any(Function), expect.anything());
    expect(container.textContent).toContain("变更前备份");
  });

  it("hides 执行 button for v0.1 runbook", async () => {
    vi.mocked(api.getRunbooks).mockResolvedValue({
      ok: true,
      data: { count: 1, runbooks: [{ name: "harbor-restart", title: "Harbor 服务异常恢复", step_count: 1 }] },
    } as never);
    vi.mocked(api.getRunbook).mockResolvedValue({ ok: true, data: V1_RUNBOOK } as never);

    const container = render(<RunbooksPage />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const runBtn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.includes("执行"));
    expect(runBtn).toBeUndefined();
  });

  it("renders execution history（runbook/时间/来源/结果）", async () => {
    vi.mocked(api.getRunbooks).mockResolvedValue({
      ok: true,
      data: { count: 1, runbooks: [{ name: "nginx-config-update", title: "Nginx 配置变更并生效", step_count: 3 }] },
    } as never);
    vi.mocked(api.getRunbook).mockResolvedValue({ ok: true, data: V2_RUNBOOK } as never);
    vi.mocked(api.getRunbookExecutions).mockResolvedValue({
      ok: true,
      data: {
        count: 2,
        executions: [
          {
            runbook: "nginx-config-update",
            ts: "2026-08-23T10:00:00+08:00",
            source: "user",
            result: "ok",
            env: "prod",
          },
          {
            runbook: "nginx-config-update",
            ts: "2026-08-23T02:00:00+08:00",
            source: "schedule",
            result: "rolled_back",
            env: "prod",
          },
        ],
      },
    } as never);

    const container = render(<RunbooksPage />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const text = container.textContent ?? "";
    expect(text).toContain("执行历史（最近 2 次）");
    expect(text).toContain("schedule");
    expect(text).toContain("已回滚");
  });
});


describe("批次八十一 RunbooksPage 锁定 + 覆盖率", () => {
  it("无实时流的锁定中执行（定时/后台）显示锁定徽标，不展开", async () => {
    vi.mocked(api.getRunbooks).mockResolvedValue({
      ok: true,
      data: { count: 1, runbooks: [{ name: "nginx-config-update", title: "Nginx 配置变更并生效", step_count: 3 }] },
    } as never);
    vi.mocked(api.getRunbook).mockResolvedValue({ ok: true, data: V2_RUNBOOK } as never);
    vi.mocked(api.getRunbookExecutions).mockResolvedValue({
      ok: true,
      data: {
        count: 0,
        executions: [],
        running: [],
        locks: [
          { exec_id: "exec_sched1", runbook: "nginx-config-update", env: "prod", started_at: "2026-08-23T10:00:00+08:00" },
        ],
      },
    } as never);

    const container = render(<RunbooksPage />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const text = container.textContent ?? "";
    expect(text).toContain("锁定中");
    expect(text).toContain("定时/后台");
    expect(api.runbookProgressStream).not.toHaveBeenCalled();
  });

  it("覆盖率区块渲染动作频率 + 覆盖状态 + 缺口", async () => {
    vi.mocked(api.getRunbooks).mockResolvedValue({
      ok: true,
      data: { count: 1, runbooks: [{ name: "nginx-config-update", title: "Nginx 配置变更并生效", step_count: 3 }] },
    } as never);
    vi.mocked(api.getRunbook).mockResolvedValue({ ok: true, data: V2_RUNBOOK } as never);
    vi.mocked(api.getRunbookCoverage).mockResolvedValue({
      ok: true,
      data: {
        high_risk: { total: 2, covered: 1, uncovered: ["reboot"], coverage_pct: 50 },
        usage: {
          window_days: 30,
          audit_events_scanned: 42,
          actions: [
            { action: "restart", use_count: 20, covered: true, runbooks: ["harbor-restart"] },
            { action: "reboot", use_count: 5, covered: false, runbooks: [] },
          ],
          total_unique: 2,
          covered_unique: 1,
          coverage_pct: 50,
          gaps: [{ action: "reboot", use_count: 5, covered: false, runbooks: [] }],
        },
      },
    } as never);

    const container = render(<RunbooksPage />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const text = container.textContent ?? "";
    expect(text).toContain("Runbook 覆盖率");
    expect(text).toContain("动作 2");
    expect(text).toContain("覆盖率 50%");
    // task19 F1：默认折叠——覆盖明细表不渲染，点「展开」后才出现。
    expect(container.querySelector('[data-testid="coverage-table"]')).toBeNull();
    const toggle = container.querySelector<HTMLButtonElement>('[data-testid="coverage-toggle"]');
    expect(toggle).toBeTruthy();
    expect(toggle!.getAttribute("aria-expanded")).toBe("false");
    await act(async () => {
      toggle!.click();
    });
    const expandedText = container.textContent ?? "";
    expect(container.querySelector('[data-testid="coverage-table"]')).toBeTruthy();
    expect(toggle!.getAttribute("aria-expanded")).toBe("true");
    expect(expandedText).toContain("restart");
    expect(expandedText).toContain("reboot");
    expect(expandedText).toContain("已覆盖");
    expect(expandedText).toContain("未覆盖");
    expect(expandedText).toContain("建议沉淀 runbook");
    expect(expandedText).toContain("reboot（5 次）");
  });

  it("覆盖率空态（无审计数据）引导不崩", async () => {
    vi.mocked(api.getRunbooks).mockResolvedValue({
      ok: true,
      data: { count: 0, runbooks: [] },
    } as never);
    const container = render(<RunbooksPage />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const text = container.textContent ?? "";
    expect(text).toContain("Runbook 覆盖率");
    expect(text).toContain("暂无审计数据");
  });
});

// ---------------------------------------------------------------------------
// task19 F2 — 缺口 → chat 桥（复制提示词 + /chat?prompt= 深链）
// ---------------------------------------------------------------------------

const COVERAGE_WITH_GAP = {
  ok: true,
  data: {
    high_risk: { total: 1, covered: 0, uncovered: ["reboot"], coverage_pct: 0 },
    usage: {
      window_days: 30,
      audit_events_scanned: 42,
      actions: [{ action: "reboot", use_count: 5, covered: false, runbooks: [] }],
      total_unique: 1,
      covered_unique: 0,
      coverage_pct: 0,
      gaps: [{ action: "reboot", use_count: 5, covered: false, runbooks: [] }],
    },
  },
} as never;

function renderWithRoutes(
  page: React.ReactElement,
  { initialEntry = "/runbooks" }: { initialEntry?: string } = {},
) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  const locations: string[] = [];
  function LocationProbe() {
    const loc = useLocation();
    locations.push(`${loc.pathname}${loc.search}`);
    return null;
  }
  act(() => {
    root.render(
      <MemoryRouter initialEntries={[initialEntry]}>
        <LocationProbe />
        <Routes>
          <Route path="/chat" element={<div data-testid="chat-target" />} />
          <Route path="*" element={page} />
        </Routes>
      </MemoryRouter>,
    );
  });
  return { container, root, locations };
}

describe("RunbooksPage coverage collapse + gap bridge (task19)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getRunbookExecutions).mockResolvedValue({
      ok: true,
      data: { count: 0, executions: [] },
    } as never);
    vi.mocked(api.getRunbooks).mockResolvedValue({
      ok: true,
      data: { count: 1, runbooks: [{ name: "reboot-drill", title: "重启演练", step_count: 1 }] },
    } as never);
    vi.mocked(api.getRunbook).mockResolvedValue({ ok: true, data: V2_RUNBOOK } as never);
    vi.mocked(api.getRunbookCoverage).mockResolvedValue(COVERAGE_WITH_GAP);
  });

  it("F1: 默认折叠（表不渲染 + 统计仍在头部），点开显示明细与缺口", async () => {
    const container = render(<RunbooksPage />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    // 头部统计保留（task19 F1：折叠后统计仍在卡片头）
    expect(container.textContent).toContain("Runbook 覆盖率");
    expect(container.textContent).toContain("动作 1");
    expect(container.textContent).toContain("扫描审计事件 42");
    expect(container.textContent).not.toContain("已复制");

    const toggle = container.querySelector<HTMLButtonElement>('[data-testid="coverage-toggle"]');
    expect(toggle).toBeTruthy();
    expect(toggle!.getAttribute("aria-expanded")).toBe("false");
    // 折叠态：覆盖明细表不渲染
    expect(container.querySelector('[data-testid="coverage-table"]')).toBeNull();

    await act(async () => {
      toggle!.click();
    });
    expect(toggle!.getAttribute("aria-expanded")).toBe("true");
    expect(container.querySelector('[data-testid="coverage-table"]')).toBeTruthy();
    expect(container.textContent).toContain("reboot");
    expect(container.textContent).toContain("高频未覆盖，建议沉淀 runbook");
  });

  it("F2: 复制按钮把可发送提示词放进剪贴板并反馈已复制", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const container = render(<RunbooksPage />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const toggle = container.querySelector<HTMLButtonElement>('[data-testid="coverage-toggle"]')!;
    await act(async () => {
      toggle.click();
    });

    const copyBtn = container.querySelector<HTMLButtonElement>('[data-testid="gap-copy-reboot"]')!;
    expect(copyBtn.textContent).toContain("复制");
    await act(async () => {
      copyBtn.click();
      await Promise.resolve();
    });
    expect(writeText).toHaveBeenCalledTimes(1);
    const prompt = writeText.mock.calls[0][0] as string;
    expect(prompt).toContain("reboot");
    expect(prompt).toContain("5 uses");
    expect(prompt).toContain("runbook_create");
    expect(container.textContent).toContain("已复制");
  });

  it("F2: 去 Chat 生成 → /chat?prompt=<编码后的提示词>", async () => {
    const { container, locations } = renderWithRoutes(<RunbooksPage />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const toggle = container.querySelector<HTMLButtonElement>('[data-testid="coverage-toggle"]')!;
    await act(async () => {
      toggle.click();
    });
    const chatBtn = container.querySelector<HTMLButtonElement>('[data-testid="gap-chat-reboot"]')!;
    expect(chatBtn.textContent).toContain("去 Chat 生成");
    await act(async () => {
      chatBtn.click();
    });
    expect(container.querySelector('[data-testid="chat-target"]')).toBeTruthy();
    const last = locations[locations.length - 1];
    expect(last.startsWith("/chat?prompt=")).toBe(true);
    const prompt = decodeURIComponent(last.slice("/chat?prompt=".length));
    expect(prompt).toContain("reboot");
    expect(prompt).toContain("5 uses");
  });
});
