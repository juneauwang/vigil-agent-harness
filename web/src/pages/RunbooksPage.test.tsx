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
});
