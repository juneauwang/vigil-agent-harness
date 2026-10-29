// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import RegistrePage, { bookstackSlug } from "./RegistrePage";
import { api } from "@/lib/api";

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
      getDigestConfig: vi.fn(),
      updateDigestConfig: vi.fn(),
      setDigestCredential: vi.fn(),
      testDigestPublish: vi.fn(),
      listDigestReports: vi.fn(),
      getDigestReport: vi.fn(),
      generateDigest: vi.fn(),
      createDigestPipeline: vi.fn(),
      listDigestRuns: vi.fn(),
    },
  };
});

const FACTS_JOB = {
  id: "job-facts",
  name: "Weekly work log (digest)",
  schedule_display: "0 18 * * 5",
  script: "builtin:digest",
  script_args: ["--window", "7d", "--format", "facts"],
  enabled: true,
  next_run_at: "2026-11-06T18:00:00+08:00",
  last_status: "ok",
  last_run_at: "2026-10-30T18:00:00+08:00",
  profile: "default",
};

const RENDER_JOB = {
  id: "job-render",
  name: "Weekly work log (render)",
  schedule_display: "0 19 * * 5",
  script: "builtin:digest",
  script_args: ["--window", "14d", "--format", "md", "--publish", "bookstack"],
  enabled: true,
  next_run_at: "2026-11-06T19:00:00+08:00",
  last_status: null,
  last_run_at: null,
  profile: "default",
};

const CONFIG_UNCONFIGURED = {
  window: "7d",
  publish: {
    target: "none",
    bookstack: { base_url: "", book_id: 0, credential_ref: "bookstack-api", configured: false },
  },
};

const CONFIG_CONFIGURED = {
  window: "7d",
  publish: {
    target: "bookstack",
    bookstack: {
      base_url: "http://bookstack.local",
      book_id: 4,
      credential_ref: "bookstack-api",
      configured: true,
    },
  },
};

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  vi.mocked(api.listCronJobs).mockResolvedValue([FACTS_JOB, RENDER_JOB] as never);
  vi.mocked(api.getDigestConfig).mockResolvedValue({ ok: true, data: CONFIG_UNCONFIGURED } as never);
  vi.mocked(api.listDigestReports).mockResolvedValue({ ok: true, data: [] } as never);
  vi.mocked(api.updateCronJob).mockResolvedValue(FACTS_JOB as never);
  vi.mocked(api.updateDigestConfig).mockResolvedValue({ ok: true, data: CONFIG_CONFIGURED } as never);
  vi.mocked(api.setDigestCredential).mockResolvedValue({
    ok: true,
    ref: "bookstack-api",
    configured: true,
  } as never);
  vi.mocked(api.getDigestReport).mockResolvedValue({
    ok: true,
    data: { name: "Vigil-作业周报-2026-W44.md", content: "# 周报内容", mtime: "", size: 10 },
  } as never);
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

describe("RegistrePage", () => {
  it("流水线卡：写稿/渲染两行；窗口不一致 → 同步按钮 → 两条 PUT", async () => {
    const container = render(<RegistrePage />);
    await flush();

    const text = container.textContent ?? "";
    expect(text).toContain("写稿");
    expect(text).toContain("渲染发布");
    expect(text).toContain("Weekly work log (digest)");
    expect(text).toContain("Weekly work log (render)");
    expect(text).toContain("0 18 * * 5");
    expect(text).toContain("14d");

    const syncBtn = button(container, "窗口同步到另一条");
    expect(syncBtn, "窗口 7d vs 14d 必须提示并给同步按钮").toBeTruthy();

    act(() => syncBtn?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await flush();

    expect(api.updateCronJob).toHaveBeenCalledTimes(2);
    expect(api.updateCronJob).toHaveBeenNthCalledWith(1, "job-facts", {
      script_args: ["--window", "7d", "--format", "facts"],
    });
    expect(api.updateCronJob).toHaveBeenNthCalledWith(2, "job-render", {
      script_args: ["--window", "7d", "--format", "md", "--publish", "bookstack"],
    });
  });


  it("空态：没有流水线 → 直接建两条（窗口/周几/几点 → 两条 job）", async () => {
    vi.mocked(api.listCronJobs).mockResolvedValue([] as never);
    vi.mocked(api.createDigestPipeline).mockResolvedValue({
      ok: true,
      data: {
        window: "7d",
        publish: "file",
        facts: { id: "new-facts", schedule: "0 18 * * 5" },
        render: { id: "new-render", schedule: "10 18 * * 5" },
      },
    } as never);

    const container = render(<RegistrePage />);
    await flush();

    expect(container.querySelector("[data-testid='registre-pipeline-empty']")).toBeTruthy();
    const createBtn = button(container, "创建流水线");
    expect(createBtn, "空态必须给一键创建入口").toBeTruthy();

    act(() => createBtn?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await flush();

    expect(api.createDigestPipeline).toHaveBeenCalledWith({
      window: "7d",
      facts_schedule: "0 18 * * 5",
      render_schedule: "10 18 * * 5",
    });
    expect(container.textContent ?? "").toContain("流水线已创建");
  });

  it("设置卡：令牌留空不发 credential；填了才发，保存后 configured 变 true", async () => {
    vi.mocked(api.getDigestConfig)
      .mockResolvedValueOnce({ ok: true, data: CONFIG_UNCONFIGURED } as never)
      .mockResolvedValueOnce({ ok: true, data: CONFIG_CONFIGURED } as never);

    const container = render(<RegistrePage />);
    await flush();

    expect(container.textContent ?? "").toContain("未配置");

    // 第一次：不填令牌 → 只保存配置
    act(() => button(container, "保存")?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await flush();
    expect(api.updateDigestConfig).toHaveBeenCalledTimes(1);
    expect(api.setDigestCredential).not.toHaveBeenCalled();

    // 第二次：填令牌 → 额外发 credential
    const tokenInput = container.querySelector('input[aria-label="令牌（token_id:token_secret）"]') as HTMLInputElement;
    expect(tokenInput).toBeTruthy();
    setInput(tokenInput, "tid:tsecret");
    act(() => button(container, "保存")?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await flush();

    expect(api.updateDigestConfig).toHaveBeenCalledTimes(2);
    expect(api.setDigestCredential).toHaveBeenCalledWith("bookstack-api", "tid:tsecret");
    expect(container.textContent ?? "").toContain("已配置");
  });

  it("台账：列表渲染 + 点预览读 /api/digest/reports/{name}", async () => {
    vi.mocked(api.listDigestReports).mockResolvedValue({
      ok: true,
      data: [
        {
          name: "Vigil-作业周报-2026-W44.md",
          path: "/tmp/vigil-home/reports/Vigil-作业周报-2026-W44.md",
          mtime: "2026-10-30T18:00:00",
          size: 2048,
          granularity: "weekly",
          posted: true,
        },
      ],
    } as never);

    const container = render(<RegistrePage />);
    await flush();

    const text = container.textContent ?? "";
    expect(text).toContain("Vigil-作业周报-2026-W44.md");
    expect(text).toContain("周报");
    expect(text).toContain("2.0 KB");

    act(() => button(container, "预览")?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await flush();

    expect(api.getDigestReport).toHaveBeenCalledWith("Vigil-作业周报-2026-W44.md");
    expect(container.textContent ?? "").toContain("周报内容");
  });

  it("bookstackSlug：中文丢弃、ASCII 小写、分隔符折叠", () => {
    expect(bookstackSlug("Vigil-作业周报-2026-W44.md")).toBe("vigil-2026-w44");
    expect(bookstackSlug("Vigil-作业日报-2026-10-28.md")).toBe("vigil-2026-10-28");
    expect(bookstackSlug("报告.md")).toBe("");
  });
});
