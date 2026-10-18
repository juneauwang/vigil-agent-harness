// @vitest-environment jsdom
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
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

function findButtonByText(container: HTMLElement, text: string): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll("button")).find(
    (button) => button.textContent?.trim() === text,
  );
}

describe("MatrixPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    document.body.innerHTML = "";
  });

  it("渲染矩阵：动作行、环境列、三态控件、来源与安全说明", async () => {
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
    // 手动改来源现在以图例/来源点表达；页面上仍能看到手动改说明
    expect(body).toContain("手动改过");
    // 顶部安全说明
    expect(body).toContain("矩阵是人工安全资产，修改即审计");
    // 漏配警告
    expect(body).toContain("漏配警告");
    expect(body).toContain("默认 approve");

    // 3 actions × 2 envs = 6 个三态控件（每个 3 个按钮）
    expect(container.querySelectorAll(".matrix-seg-group").length).toBe(6);
    expect(container.querySelectorAll(".matrix-seg").length).toBe(18);

    // 缺省格子使用虚线来源标记
    const missingDots = container.querySelectorAll('.matrix-source-dot[data-source="missing"]');
    expect(missingDots.length).toBeGreaterThan(0);
  });

  it("修改格子：乐观更新 + PUT 落库 + 成功 toast", async () => {
    vi.mocked(api.getMatrix).mockResolvedValue({ ok: true, data: MATRIX as never });
    vi.mocked(api.setMatrixCell).mockResolvedValue({ ok: true, changed: true });
    const container = await renderPage();
    await flushPromises();

    const cell = container.querySelector<HTMLElement>('[data-action="restart"][data-env="prod"]');
    expect(cell).not.toBeNull();
    const approveButton = cell?.querySelector<HTMLButtonElement>('button[data-level="approve"]');
    expect(approveButton).not.toBeNull();

    await act(async () => {
      approveButton?.click();
    });

    expect(api.setMatrixCell).toHaveBeenCalledWith("prod", "restart", "approve");
    expect(container.textContent ?? "").toContain("已更新 prod.restart → approve");
  });

  it("保存失败：回滚乐观更新并显示错误", async () => {
    vi.mocked(api.getMatrix).mockResolvedValue({ ok: true, data: MATRIX as never });
    vi.mocked(api.setMatrixCell).mockResolvedValue({ ok: false, error: "write_failed" });
    const container = await renderPage();
    await flushPromises();

    const cell = container.querySelector<HTMLElement>('[data-action="restart"][data-env="prod"]');
    const executeButton = cell?.querySelector<HTMLButtonElement>('button[data-level="execute"]');
    await act(async () => {
      executeButton?.click();
    });

    expect(container.textContent ?? "").toContain("write_failed");
    // 回滚为 required：required 按钮仍为选中态
    const requiredButton = cell?.querySelector<HTMLButtonElement>('button[data-level="required"]');
    expect(requiredButton?.getAttribute("data-selected")).toBe("true");
  });

  it("空态：显示四模板引导，模板 1/2/3 直接 init", async () => {
    vi.mocked(api.getMatrix).mockResolvedValue({ ok: true, data: EMPTY as never });
    vi.mocked(api.initMatrix).mockResolvedValue({ ok: true, data: MATRIX as never });
    const container = await renderPage();
    await flushPromises();

    expect(container.textContent ?? "").toContain("尚未生成操作矩阵");
    expect(container.textContent ?? "").toContain("模板 4 · 自定义");
    const template1 = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("模板 1") ?? false,
    );
    await act(async () => {
      template1?.click();
    });
    expect(api.initMatrix).toHaveBeenCalledWith("template1", undefined);
  });

  it("模板 4 级联：execute 集 → approve 集 → 确认页 → 生成", async () => {
    vi.mocked(api.getMatrix).mockResolvedValue({ ok: true, data: EMPTY as never });
    vi.mocked(api.initMatrix).mockResolvedValue({ ok: true, data: MATRIX as never });
    const container = await renderPage();
    await flushPromises();

    const template4 = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("模板 4") ?? false,
    );
    await act(async () => {
      template4?.click();
    });
    expect(container.textContent ?? "").toContain("模板 4 · 自定义级联");

    // 第 1 步：选 execute 集
    const checkboxes = () =>
      Array.from(container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'));
    await act(async () => {
      checkboxes().find((checkbox) => checkbox.parentElement?.textContent?.includes("query"))?.click();
    });
    await act(async () => {
      findButtonByText(container, "下一步")?.click();
    });

    // 第 2 步：query 已从 approve 选项移除，选 reboot
    const approveLabels = Array.from(container.querySelectorAll("label"));
    const rebootLabel = approveLabels.find((label) => label.textContent?.includes("reboot") ?? false);
    await act(async () => {
      rebootLabel?.querySelector<HTMLInputElement>('input[type="checkbox"]')?.click();
    });
    await act(async () => {
      findButtonByText(container, "下一步")?.click();
    });

    // 第 3 步：确认页 → 生成
    expect(container.textContent ?? "").toContain("确认三档分配");
    await act(async () => {
      findButtonByText(container, "生成矩阵")?.click();
    });
    expect(api.initMatrix).toHaveBeenCalledWith("template4", {
      execute: ["query"],
      approve: ["reboot"],
    });
  });
});
