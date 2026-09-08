// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { ApiError, type YamlSaveWarning } from "@/lib/api";
import YamlEditorDrawer, { type YamlEditorTarget } from "./YamlEditorDrawer";

// CodeMirror 6 在 jsdom 下缺若干 DOM API（Range/getBoundingClientRect 等）——
// 抽屉逻辑测试用 textarea 桩替身（CM 集成本身由 tsc/build + 浏览器验收覆盖）。
vi.mock("@uiw/react-codemirror", async () => {
  const { createElement } = await import("react");
  return {
    default: (props: { value: string; onChange: (v: string) => void }) =>
      createElement("textarea", {
        "aria-label": "yaml-stub",
        value: props.value,
        onChange: (e: { target: { value: string } }) => props.onChange(e.target.value),
      }),
  };
});

/** React 受控 textarea 的原生 setter + input 事件（绕过值跟踪去重）。 */
function setNativeValue(el: HTMLTextAreaElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    "value",
  )?.set;
  setter?.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

const SOURCE = "# comment\nname: harbor-restart\ntitle: demo\n";

function makeTarget(overrides?: Partial<YamlEditorTarget>): YamlEditorTarget {
  return {
    title: "harbor-restart",
    subtitle: "runbooks/harbor-restart.yaml",
    load: vi.fn().mockResolvedValue(SOURCE),
    save: vi.fn().mockResolvedValue({ warnings: [] }),
    ...overrides,
  };
}

function mount(target: YamlEditorTarget | null, handlers: { onClose?: () => void; onSaved?: (w: YamlSaveWarning[]) => void } = {}) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => {
    root.render(
      <YamlEditorDrawer
        target={target}
        onClose={handlers.onClose ?? (() => {})}
        onSaved={handlers.onSaved}
      />,
    );
  });
  return { container, root };
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

describe("YamlEditorDrawer", () => {
  it("打开抽屉加载原文并显示（含注释行）", async () => {
    const target = makeTarget();
    const { container, root } = mount(target);
    try {
      await flush();
      const textarea = container.querySelector("textarea");
      expect(textarea).toBeTruthy();
      expect((textarea as HTMLTextAreaElement).value).toBe(SOURCE);
      expect(container.textContent).toContain("runbooks/harbor-restart.yaml");
      expect(target.load).toHaveBeenCalledOnce();
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });

  it("保存成功：调用 save（提交编辑后文本）→ onSaved + onClose", async () => {
    const target = makeTarget();
    const onClose = vi.fn();
    const onSaved = vi.fn();
    const { container, root } = mount(target, { onClose, onSaved });
    try {
      await flush();
      const textarea = container.querySelector("textarea") as HTMLTextAreaElement;
      await act(async () => {
        setNativeValue(textarea, SOURCE.replace("demo", "demo-v2"));
      });
      const saveBtn = container.querySelector('[data-testid="yaml-editor-save"]') as HTMLButtonElement;
      await act(async () => {
        saveBtn.click();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(target.save).toHaveBeenCalledWith(SOURCE.replace("demo", "demo-v2"));
      expect(onSaved).toHaveBeenCalledOnce();
      expect(onClose).toHaveBeenCalledOnce();
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });

  it("校验失败（422 errors）：错误列表带行号内联渲染，抽屉不关闭、不回调 onSaved", async () => {
    const target = makeTarget({
      save: vi.fn().mockRejectedValue(
        new ApiError("validation_failed", "校验失败", 422, {
          errors: [{ line: 3, message: "title 不能为空" }],
        }),
      ),
    });
    const onClose = vi.fn();
    const onSaved = vi.fn();
    const { container, root } = mount(target, { onClose, onSaved });
    try {
      await flush();
      const saveBtn = container.querySelector('[data-testid="yaml-editor-save"]') as HTMLButtonElement;
      await act(async () => {
        saveBtn.click();
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      const errorsBox = container.querySelector('[data-testid="yaml-editor-errors"]');
      expect(errorsBox).toBeTruthy();
      expect(errorsBox?.textContent).toContain("第 3 行");
      expect(errorsBox?.textContent).toContain("title 不能为空");
      expect(onClose).not.toHaveBeenCalled();
      expect(onSaved).not.toHaveBeenCalled();
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });

  it("保存成功但带告警（自动执行授权失效）：警示条渲染，抽屉保持打开", async () => {
    const target = makeTarget({
      save: vi.fn().mockResolvedValue({
        warnings: [{ code: "auto_exec_approval_invalidated", message: "runbook 内容自审批后已被修改" }],
      }),
    });
    const onClose = vi.fn();
    const { container, root } = mount(target, { onClose });
    try {
      await flush();
      const saveBtn = container.querySelector('[data-testid="yaml-editor-save"]') as HTMLButtonElement;
      await act(async () => {
        saveBtn.click();
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      const warnBox = container.querySelector('[data-testid="yaml-editor-warnings"]');
      expect(warnBox).toBeTruthy();
      expect(warnBox?.textContent).toContain("自动执行授权已失效");
      expect(warnBox?.textContent).toContain("runbook 内容自审批后已被修改");
      // 成功 + 告警 → 抽屉保持打开，由用户显式关闭
      expect(onClose).not.toHaveBeenCalled();
      const closeBtn = container.querySelector('[data-testid="yaml-editor-warnings-close"]') as HTMLButtonElement;
      act(() => closeBtn.click());
      expect(onClose).toHaveBeenCalledOnce();
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });

  it("脏状态关闭需确认：confirm 返回 false → 不关闭", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    const target = makeTarget();
    const onClose = vi.fn();
    const { container, root } = mount(target, { onClose });
    try {
      await flush();
      const textarea = container.querySelector("textarea") as HTMLTextAreaElement;
      await act(async () => {
        setNativeValue(textarea, SOURCE + "# dirty\n");
      });
      const cancelBtn = [...container.querySelectorAll("button")].find(
        (b) => b.textContent === "取消",
      ) as HTMLButtonElement;
      act(() => cancelBtn.click());
      expect(confirmSpy).toHaveBeenCalledOnce();
      expect(onClose).not.toHaveBeenCalled();
    } finally {
      act(() => root.unmount());
      container.remove();
      confirmSpy.mockRestore();
    }
  });

  it("干净状态直接关闭（不弹确认）", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    const target = makeTarget();
    const onClose = vi.fn();
    const { container, root } = mount(target, { onClose });
    try {
      await flush();
      const cancelBtn = [...container.querySelectorAll("button")].find(
        (b) => b.textContent === "取消",
      ) as HTMLButtonElement;
      act(() => cancelBtn.click());
      expect(confirmSpy).not.toHaveBeenCalled();
      expect(onClose).toHaveBeenCalledOnce();
    } finally {
      act(() => root.unmount());
      container.remove();
      confirmSpy.mockRestore();
    }
  });
});
