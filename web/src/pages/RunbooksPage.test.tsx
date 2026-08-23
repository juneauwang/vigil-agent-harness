// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
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

  it("shows v0.2 runbook with 执行 button and runs it（确认 → 结果分步展示）", async () => {
    vi.mocked(api.getRunbooks).mockResolvedValue({
      ok: true,
      data: { count: 1, runbooks: [{ name: "nginx-config-update", title: "Nginx 配置变更并生效", step_count: 3 }] },
    } as never);
    vi.mocked(api.getRunbook).mockResolvedValue({ ok: true, data: V2_RUNBOOK } as never);
    vi.mocked(api.runRunbook).mockResolvedValue({
      ok: true,
      data: {
        runbook: "nginx-config-update",
        result: "ok",
        env: "prod",
        ts: "2026-08-23T10:00:00+08:00",
        duration_s: 1.24,
        steps: [
          { id: "backup", action: "backup", status: "ok", ok: true },
          { id: "apply", action: "apply_config", status: "ok", ok: true },
          { id: "verify", action: "verify", status: "ok", ok: true },
        ],
      },
    } as never);

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
    const text = container.textContent ?? "";
    expect(text).toContain("执行结果");
    expect(text).toContain("成功");
    expect(text).toContain("backup");
    expect(text).toContain("apply_config");
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
