// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import MatrixPage from "./MatrixPage";
import { api } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getMatrix: vi.fn(),
      setMatrixCell: vi.fn(),
      initMatrix: vi.fn(),
    },
  };
});

const MATRIX: Record<string, unknown> = {
  schema_version: 1,
  source: "template2",
  base_template: "template2",
  actions: ["restart", "query", "reboot"],
  matrix: {
    prod: { restart: "required", query: "execute", reboot: "required" },
    local: { restart: "execute", query: "execute", reboot: "approve" },
  },
  sources: {
    prod: { restart: "template2", query: "template2", reboot: "template2" },
    local: { restart: "template2", query: "manual", reboot: "template2" },
  },
  warnings: ["matrix.prod 漏配 1 个动作（backup）——默认 approve（保守）。"],
};

const EMPTY: Record<string, unknown> = {
  schema_version: 1,
  source: "",
  base_template: "",
  actions: ["restart", "query", "reboot"],
  matrix: {},
  sources: {},
  warnings: ["矩阵文件不存在——全部动作默认 approve（保守）。"],
};

async function renderPage() {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter>
        <MatrixPage />
      </MemoryRouter>,
    );
  });
  return container;
}

function flushPromises() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

describe("MatrixPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    document.body.innerHTML = "";
  });

  it("渲染表格：行=动作、列=环境、格=三态下拉 + 来源展示", async () => {
    vi.mocked(api.getMatrix).mockResolvedValue({ ok: true, data: MATRIX as never });
    const container = await renderPage();
    await flushPromises();

    const body = container.textContent ?? "";
    expect(body).toContain("操作矩阵");
    expect(body).toContain("restart");
    expect(body).toContain("query");
    expect(body).toContain("prod");
    expect(body).toContain("local");
    expect(body).toContain("template2");
    expect(body).toContain("· 手动改");
    // 顶部安全说明
    expect(body).toContain("矩阵是人工安全资产，修改即审计");
    // 漏配警告
    expect(body).toContain("漏配警告");
    expect(body).toContain("默认 approve");
  });

  it("修改格子：乐观更新 + PUT 落库", async () => {
    vi.mocked(api.getMatrix).mockResolvedValue({ ok: true, data: MATRIX as never });
    vi.mocked(api.setMatrixCell).mockResolvedValue({ ok: true, changed: true });
    const container = await renderPage();
    await flushPromises();

    const selects = document.querySelectorAll("select");
    expect(selects.length).toBe(6); // 3 actions × 2 envs
    const firstCell = selects[0]; // prod.restart
    await act(async () => {
      firstCell.value = "approve";
      firstCell.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(api.setMatrixCell).toHaveBeenCalledWith("prod", "restart", "approve");
    expect(container.textContent ?? "").toContain("已更新 prod.restart → approve");
  });

  it("空态：显示四模板引导，模板 1/2/3 直接 init", async () => {
    vi.mocked(api.getMatrix).mockResolvedValue({ ok: true, data: EMPTY as never });
    vi.mocked(api.initMatrix).mockResolvedValue({ ok: true, data: MATRIX as never });
    const container = await renderPage();
    await flushPromises();

    expect(container.textContent ?? "").toContain("尚未生成操作矩阵");
    expect(container.textContent ?? "").toContain("模板 4 · 自定义");
    const buttons = Array.from(document.querySelectorAll("button"));
    const t1 = buttons.find((b) => b.textContent?.includes("模板 1") ?? false);
    await act(async () => {
      t1?.click();
    });
    expect(api.initMatrix).toHaveBeenCalledWith("template1", undefined);
  });

  it("模板 4 级联：execute 集 → approve 集（已选移除）→ 生成", async () => {
    vi.mocked(api.getMatrix).mockResolvedValue({ ok: true, data: EMPTY as never });
    vi.mocked(api.initMatrix).mockResolvedValue({ ok: true, data: MATRIX as never });
    const container = await renderPage();
    await flushPromises();

    // 打开模板 4 modal
    const buttons = Array.from(document.querySelectorAll("button"));
    const t4 = buttons.find((b) => b.textContent?.includes("模板 4") ?? false);
    await act(async () => {
      t4?.click();
    });
    expect(container.textContent ?? "").toContain("模板 4 · 自定义级联");

    // 第 1 步：选 execute 集
    const checkboxes = () => Array.from(document.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'));
    await act(async () => {
      checkboxes().find((c) => c.parentElement?.textContent?.includes("query"))?.click();
    });
    const nextBtn = Array.from(document.querySelectorAll("button")).find((b) => b.textContent === "下一步");
    await act(async () => {
      nextBtn?.click();
    });
    // 第 2 步：query 已从 approve 选项移除，选 reboot
    const approveLabels = Array.from(document.querySelectorAll("label"));
    const rebootLabel = approveLabels.find((l) => l.textContent?.includes("reboot") ?? false);
    await act(async () => {
      (rebootLabel?.querySelector('input[type="checkbox"]') as HTMLInputElement | null)?.click();
    });
    expect(container.textContent ?? "").toContain("restart");
    const genBtn = Array.from(document.querySelectorAll("button")).find((b) => b.textContent === "生成矩阵");
    await act(async () => {
      genBtn?.click();
    });
    expect(api.initMatrix).toHaveBeenCalledWith("template4", {
      execute: ["query"],
      approve: ["reboot"],
    });
  });
});
