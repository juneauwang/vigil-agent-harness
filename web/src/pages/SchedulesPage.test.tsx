// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import SchedulesPage from "./SchedulesPage";
import { withWindowArg, windowArg } from "@/lib/schedules";
import { api, ApiError } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      listCronJobs: vi.fn(),
      updateCronJob: vi.fn(),
      pauseCronJob: vi.fn(),
      resumeCronJob: vi.fn(),
      runCronJobNow: vi.fn(),
      triggerCronJob: vi.fn(),
      deleteCronJob: vi.fn(),
      listBlueprints: vi.fn(),
      instantiateBlueprint: vi.fn(),
    },
  };
});

const DIGEST_JOB = {
  id: "job-digest",
  name: "作业周报",
  schedule: { kind: "cron" as const, display: "0 18 * * 5" },
  schedule_display: "0 18 * * 5",
  script: "builtin:digest",
  script_args: ["--window", "7d", "--format", "facts"],
  no_agent: true,
  enabled: true,
  state: "scheduled",
  next_run_at: "2026-11-06T18:00:00+08:00",
  last_status: "ok",
  last_run_at: "2026-10-30T18:00:00+08:00",
  profile: "default",
};

const PLAIN_JOB = {
  id: "job-plain",
  name: "巡检",
  schedule: { kind: "interval" as const, display: "every 10m", minutes: 10 },
  schedule_display: "every 10m",
  script: null,
  enabled: true,
  state: "scheduled",
  next_run_at: "2026-10-28T21:10:00+08:00",
  last_status: null,
  last_run_at: null,
  profile: "default",
};

const BLUEPRINT = {
  key: "daily-report",
  title: "Daily report",
  description: "每天汇总一次",
  fields: [
    { name: "time", type: "time", label: "What time?", default: "18:00" },
    {
      name: "window",
      type: "enum",
      label: "Window",
      default: "7d",
      options: ["7d", "14d", "30d"],
      strict: false,
    },
    {
      name: "publish",
      type: "enum",
      label: "Publish to",
      default: "file",
      options: ["bookstack", "file", "none"],
    },
  ],
};

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  vi.mocked(api.listCronJobs).mockResolvedValue([DIGEST_JOB, PLAIN_JOB] as never);
  vi.mocked(api.listBlueprints).mockResolvedValue({ blueprints: [BLUEPRINT] } as never);
  vi.mocked(api.updateCronJob).mockResolvedValue(DIGEST_JOB as never);
  vi.mocked(api.pauseCronJob).mockResolvedValue({ ...DIGEST_JOB, enabled: false, state: "paused" } as never);
  vi.mocked(api.resumeCronJob).mockResolvedValue(DIGEST_JOB as never);
  vi.mocked(api.deleteCronJob).mockResolvedValue({ ok: true } as never);
  vi.mocked(api.instantiateBlueprint).mockResolvedValue({ id: "job-new", name: "作业周报" } as never);
});

function render(page: React.ReactElement) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => root.render(<MemoryRouter>{page}</MemoryRouter>));
  return container;
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function button(container: HTMLElement, label: string): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll("button")).find(
    (item) => (item.textContent ?? "").trim() === label || item.getAttribute("aria-label") === label,
  ) as HTMLButtonElement | undefined;
}

function setInput(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
  act(() => {
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("SchedulesPage", () => {
  it("列表渲染：名称/计划/下次/上次状态都在（只列非 digest 任务）", async () => {
    const container = render(<SchedulesPage />);
    await flush();

    const text = container.textContent ?? "";
    expect(text).toContain("巡检");
    expect(text).toContain("every 10m");
    expect(text).toContain("2026-10-28");
    expect(text).toContain("还没跑过");
    // digest 流水线归 /registre：它的行、计划、改窗口按钮都不该出现
    expect(text).not.toContain("作业周报");
    expect(text).not.toContain("0 18 * * 5");
    const windowButtons = Array.from(container.querySelectorAll("button")).filter(
      (item) => (item.textContent ?? "").trim() === "改窗口",
    );
    expect(windowButtons).toHaveLength(0);
    expect(api.listCronJobs).toHaveBeenCalledWith("all");
  });

  it("过滤：mock digest + 普通 job → 表里只剩普通那条，页头有 Le Registre 链接", async () => {
    const container = render(<SchedulesPage />);
    await flush();

    const rows = Array.from(container.querySelectorAll("tbody tr"));
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain("巡检");

    const link = container.querySelector('a[href="/registre"]');
    expect(link, "页头必须给出 Le Registre 入口").toBeTruthy();
    expect(link?.textContent ?? "").toContain("Le Registre");
  });

  it("改计划：保存后 PUT body 含 {schedule: \"every 5m\"}", async () => {
    vi.mocked(api.updateCronJob).mockResolvedValue({
      ...PLAIN_JOB,
      schedule: { kind: "interval", display: "every 5m", minutes: 5 },
      schedule_display: "every 5m",
    } as never);
    const container = render(<SchedulesPage />);
    await flush();

    act(() => button(container, "编辑计划")?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    const input = container.querySelector('input[aria-label="计划"]') as HTMLInputElement;
    expect(input.value).toBe("every 10m");
    setInput(input, "every 5m");
    act(() => button(container, "保存")?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await flush();

    expect(api.updateCronJob).toHaveBeenCalledWith("job-plain", { schedule: "every 5m" });
    expect(container.textContent ?? "").toContain("已保存");
  });

  it("暂停：调 POST pause，行状态变成「已暂停」", async () => {
    vi.mocked(api.pauseCronJob).mockResolvedValue({
      ...PLAIN_JOB,
      enabled: false,
      state: "paused",
    } as never);
    const container = render(<SchedulesPage />);
    await flush();

    act(() => button(container, "暂停")?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await flush();

    expect(api.pauseCronJob).toHaveBeenCalledWith("job-plain");
    expect(container.textContent ?? "").toContain("已暂停");
    expect(button(container, "恢复")).toBeTruthy();
  });

  it("错误展示：PUT 400 → 页面显示后端 message，不静默失败", async () => {
    vi.mocked(api.updateCronJob).mockRejectedValue(
      new ApiError("bad_request", "Invalid schedule: bad", 400),
    );
    const container = render(<SchedulesPage />);
    await flush();

    act(() => button(container, "编辑计划")?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    const input = container.querySelector('input[aria-label="计划"]') as HTMLInputElement;
    setInput(input, "bad");
    act(() => button(container, "保存")?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await flush();

    expect(container.textContent ?? "").toContain("Invalid schedule: bad");
  });

  it("新建蓝图：按 slots 渲染表单，提交 body 为 {blueprint, values}", async () => {
    const container = render(<SchedulesPage />);
    await flush();

    act(() => button(container, "新建")?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await flush();

    expect(api.listBlueprints).toHaveBeenCalled();
    // strict=false 的 enum（digest window）= datalist input：建议 7d/14d/30d，也收 1d。
    const windowInput = container.querySelector('input[aria-label="Window"]') as HTMLInputElement;
    expect(windowInput).toBeTruthy();
    expect(windowInput.value).toBe("7d");
    expect(windowInput.getAttribute("list")).toBe("blueprint-window");
    // 自由文本 / time 槽是 input，strict enum 槽是 select
    expect(container.querySelector('input[aria-label="What time?"]')).toBeTruthy();
    expect(container.querySelector('select[aria-label="Publish to"]')).toBeTruthy();

    setInput(windowInput, "1d");
    act(() => button(container, "创建")?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await flush();

    expect(api.instantiateBlueprint).toHaveBeenCalledWith("daily-report", {
      time: "18:00",
      window: "1d",
      publish: "file",
    });
  });
});

describe("window arg helpers", () => {
  it("withWindowArg 只动 --window（替换/插入/删除）", () => {
    const base = ["--window", "7d", "--format", "facts"];
    expect(withWindowArg(base, "1d")).toEqual(["--window", "1d", "--format", "facts"]);
    expect(withWindowArg(["--format", "facts"], "2w")).toEqual(["--window", "2w", "--format", "facts"]);
    expect(withWindowArg(base, "  ")).toEqual(["--format", "facts"]);
    expect(windowArg(base)).toBe("7d");
    expect(windowArg(["--format", "facts"])).toBe("");
  });
});

describe("立即跑（run-now）", () => {
  it("走 /run-now（不是排到下一个 tick），并提示已开始执行", async () => {
    (api.listCronJobs as unknown as ReturnType<typeof vi.fn>).mockResolvedValue([PLAIN_JOB]);
    (api.runCronJobNow as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ started: true });
    const container = document.createElement("div");
    const root = createRoot(container);
    await act(async () => {
      root.render(<MemoryRouter><SchedulesPage /></MemoryRouter>);
    });
    const btn = Array.from(container.querySelectorAll("button"))
      .find((b) => (b.getAttribute("aria-label") ?? "").includes("立即跑"));
    expect(btn).toBeTruthy();
    await act(async () => { btn!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(api.runCronJobNow).toHaveBeenCalledWith("job-plain");
    expect(api.triggerCronJob).not.toHaveBeenCalled();
    root.unmount();
  });
});

describe("queue-for-next-tick", () => {
  it("uses triggerCronJob (not run-now) and shows the queued notice", async () => {
    (api.listCronJobs as unknown as ReturnType<typeof vi.fn>).mockResolvedValue([PLAIN_JOB]);
    (api.triggerCronJob as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ...PLAIN_JOB });
    const container = document.createElement("div");
    const root = createRoot(container);
    await act(async () => {
      root.render(<MemoryRouter><SchedulesPage /></MemoryRouter>);
    });
    const btn = Array.from(container.querySelectorAll("button"))
      .find((b) => (b.getAttribute("aria-label") ?? "").includes("下一轮"));
    expect(btn).toBeTruthy();
    await act(async () => { btn!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(api.triggerCronJob).toHaveBeenCalledWith("job-plain");
    expect(api.runCronJobNow).not.toHaveBeenCalled();
    root.unmount();
  });
});

describe("digest 蓝图不在 /schedules 列出", () => {
  it("weekly-oplog 被过滤（流水线统一在 /registre 建）", async () => {
    const container = render(<SchedulesPage />);
    await flush();
    act(() => button(container, "新建")?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await flush();
    const options = Array.from(container.querySelectorAll("option")).map(
      (item) => (item.textContent ?? "").trim(),
    );
    expect(options).not.toContain("Weekly work log (digest)");
    expect(options).toContain("Daily report");
  });
});
