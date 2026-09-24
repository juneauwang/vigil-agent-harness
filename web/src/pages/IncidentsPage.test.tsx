// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import IncidentsPage from "./IncidentsPage";
import { api, ApiError } from "@/lib/api";
import type {
  AutodispatchAuditEntry,
  AutodispatchAuditResponse,
  IncidentItem,
  IncidentsResponse,
} from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getIncidents: vi.fn(),
      markIncident: vi.fn(),
      getAutodispatchAudit: vi.fn(),
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

const EMPTY_AUDIT: AutodispatchAuditResponse = {
  ok: true,
  data: { count: 0, entries: [] },
};

function auditResponse(entries: AutodispatchAuditEntry[]): AutodispatchAuditResponse {
  return { ok: true, data: { count: entries.length, entries } };
}

async function mountPage(
  resp: IncidentsResponse,
  audit: AutodispatchAuditResponse = EMPTY_AUDIT,
) {
  vi.mocked(api.getIncidents).mockResolvedValue(resp);
  vi.mocked(api.getAutodispatchAudit).mockResolvedValue(audit);
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
    vi.mocked(api.getAutodispatchAudit).mockReset();
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
    vi.mocked(api.getAutodispatchAudit).mockReset();
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


// ---------------------------------------------------------------------------
// task35 PART C：自动派发记录（告警驱动的 runbook 自动执行，无会话 → 本页回看）
// ---------------------------------------------------------------------------

const AUDIT_OK: AutodispatchAuditEntry = {
  ts: "2026-09-24T01:00:00+0800",
  alertname: "HarborHealthcheckDown",
  severity: "critical",
  instance: "harbor:443",
  runbook: "harbor-restart",
  matched_keyword: "harbor healthcheck",
  result: "ok",
  needs_human: false,
  duration_s: 2.13,
  steps: [
    {
      id: "s1",
      action: "query",
      status: "ok",
      ok: true,
      target: "harbor",
      commands: [
        {
          desc: "docker ps --filter name=harbor",
          exit_code: 0,
          stdout: "harbor is healthy",
          stderr: "",
        },
      ],
    },
    {
      id: "s2",
      action: "restart",
      status: "ok",
      ok: true,
      commands: [{ desc: "compose restart", exit_code: 0, stdout: "restarted", stderr: "" }],
    },
  ],
};

const AUDIT_NEEDS_HUMAN: AutodispatchAuditEntry = {
  ts: "2026-09-24T02:00:00+0800",
  alertname: "DiskPressure",
  severity: "warning",
  instance: "node2:9100",
  runbook: "disk-cleanup",
  result: "blocked",
  needs_human: true,
  error: "资产审批未通过（预审标记缺失）",
  duration_s: 0.01,
  steps: [],
};

function buttonContaining(container: HTMLElement, label: string): HTMLButtonElement {
  const found = Array.from(container.querySelectorAll("button")).find((b) =>
    (b.textContent ?? "").includes(label),
  );
  if (!found) throw new Error(`button containing not found: ${label}`);
  return found as HTMLButtonElement;
}

async function openAutoDispatchTab(container: HTMLElement) {
  // tab 按钮文本带计数徽标（如「自动派发记录2」）→ 用包含匹配。
  await click(buttonContaining(container, "自动派发记录"));
}

describe("Incidents 自动派发记录（task35 PART C）", () => {
  beforeEach(() => {
    vi.mocked(api.getIncidents).mockReset();
    vi.mocked(api.markIncident).mockReset();
    vi.mocked(api.getAutodispatchAudit).mockReset();
  });

  it("审计为空 → 明确空态说明（不是空白区）", async () => {
    const { container, cleanup } = await mountPage(response([]), EMPTY_AUDIT);
    await openAutoDispatchTab(container);
    expect(container.textContent).toContain("暂无自动派发记录");
    expect(container.textContent).toContain("alert_autodispatch.jsonl");
    cleanup();
  });

  it("有记录 → 列出时间/告警/实例/runbook/result/耗时", async () => {
    const { container, cleanup } = await mountPage(
      response([]),
      auditResponse([AUDIT_OK, AUDIT_NEEDS_HUMAN]),
    );
    await openAutoDispatchTab(container);
    const text = container.textContent ?? "";
    expect(text).not.toContain("HighCPU"); // 告警列表未串台
    expect(text).toContain("HarborHealthcheckDown");
    expect(text).toContain("2026-09-24T01:00:00+0800");
    expect(text).toContain("harbor:443");
    expect(text).toContain("harbor-restart");
    expect(text).toContain("成功"); // result ok → 复用 runbooks 词表
    expect(text).toContain("2.13s");
    expect(text).toContain("harbor healthcheck"); // 命中关键词
    cleanup();
  });

  it("展开看每步：步骤标识 + 结果 + 截断后的输出", async () => {
    const { container, cleanup } = await mountPage(response([]), auditResponse([AUDIT_OK]));
    await openAutoDispatchTab(container);
    // 展开前不显示步骤内容
    expect(container.textContent).not.toContain("harbor is healthy");
    await click(buttonByText(container, "2 步"));
    const text = container.textContent ?? "";
    expect(text).toContain("s1");
    expect(text).toContain("s2");
    expect(text).toContain("docker ps --filter name=harbor");
    expect(text).toContain("harbor is healthy");
    expect(text).toContain("restarted");
    cleanup();
  });

  it("needs_human 视觉/文案明显不同（需人工徽标 + 说明）", async () => {
    const { container, cleanup } = await mountPage(
      response([]),
      auditResponse([AUDIT_OK, AUDIT_NEEDS_HUMAN]),
    );
    await openAutoDispatchTab(container);
    const text = container.textContent ?? "";
    expect(text).toContain("需人工");
    expect(text).toContain("已降级回建议闭环");
    // 只有 needs_human 那条带徽标 → 计数 = 1
    const badges = Array.from(container.querySelectorAll("span")).filter((el) =>
      (el.textContent ?? "").includes("需人工"),
    );
    expect(badges.length).toBe(1);
    cleanup();
  });

  it("展开 needs_human 记录 → 显示「无可回看步骤」而非空白", async () => {
    const { container, cleanup } = await mountPage(
      response([]),
      auditResponse([AUDIT_NEEDS_HUMAN]),
    );
    await openAutoDispatchTab(container);
    await click(buttonByText(container, "0 步"));
    expect(container.textContent).toContain("该次派发无可回看的步骤");
    cleanup();
  });

  it("总长截断哨兵步骤 → 显示省略步数说明", async () => {
    const entry: AutodispatchAuditEntry = {
      ...AUDIT_OK,
      steps: [AUDIT_OK.steps![0], { id: "__truncated__", omitted_steps: 7 }],
    };
    const { container, cleanup } = await mountPage(response([]), auditResponse([entry]));
    await openAutoDispatchTab(container);
    await click(buttonByText(container, "2 步"));
    expect(container.textContent).toContain("另有 7 步因超长未记录");
    cleanup();
  });
});
