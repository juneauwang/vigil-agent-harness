// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import ChatPage from "./ChatPage";
import { api } from "@/lib/api";
import type { ChatSessionSummary, ChatHistoryMessage } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      listChatSessions: vi.fn(),
      getModels: vi.fn(),
      getChatHistory: vi.fn(),
      createChatSession: vi.fn(),
      chatStream: vi.fn(),
      interruptChatSession: vi.fn(),
      setChatSessionModel: vi.fn(),
    },
  };
});

const SESSION_A: ChatSessionSummary = { id: "A", title: "会话A", created_at: "", busy: true };
const SESSION_B: ChatSessionSummary = { id: "B", title: "会话B", created_at: "", busy: false };
const HISTORY: ChatHistoryMessage[] = [
  { id: 1, role: "user", content: "看下拓扑", tools: [] },
  { id: 2, role: "assistant", content: "正在处理中", tools: [], reasoning: "正在思考" },
];

const MODELS = {
  models: [
    { id: "m-fast", name: "m-fast", description: "快/省小模型", tag: "快/省", default: true },
    { id: "m-strong", name: "m-strong", description: "强推理大模型", tag: "强/慢", default: false },
  ],
  provider: "openrouter",
  default_model: "m-fast",
};

const apiMock = api as unknown as {
  listChatSessions: ReturnType<typeof vi.fn>;
  getModels: ReturnType<typeof vi.fn>;
  getChatHistory: ReturnType<typeof vi.fn>;
  createChatSession: ReturnType<typeof vi.fn>;
  chatStream: ReturnType<typeof vi.fn>;
  interruptChatSession: ReturnType<typeof vi.fn>;
  setChatSessionModel: ReturnType<typeof vi.fn>;
};

if (typeof globalThis.HTMLElement !== "undefined" && !HTMLElement.prototype.scrollIntoView) {
  HTMLElement.prototype.scrollIntoView = () => {};
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.useFakeTimers();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => {
    root.unmount();
  });
  container.remove();
  vi.useRealTimers();
  vi.clearAllMocks();
});

async function mountWith(
  sessions: ChatSessionSummary[],
  busySessionId?: string,
  historyFor?: (id: string) => ChatHistoryMessage[],
) {
  const busy = new Set(busySessionId ? [busySessionId] : sessions.filter((s) => s.busy).map((s) => s.id));
  apiMock.getModels.mockResolvedValue(MODELS);
  apiMock.listChatSessions.mockImplementation(async () => ({
    sessions: sessions.map((s) => ({ ...s, busy: busy.has(s.id) })),
  }));
  apiMock.getChatHistory.mockImplementation(async (id: string) => ({
    chat_session_id: id,
    messages: historyFor ? historyFor(id) : id === "A" ? HISTORY : [],
    busy: busy.has(id),
  }));
  apiMock.createChatSession.mockResolvedValue({ chat_session_id: "C", created_at: "", model: "m-fast" });
  apiMock.chatStream.mockResolvedValue(undefined);
  apiMock.interruptChatSession.mockResolvedValue({ status: "interrupted" });
  apiMock.setChatSessionModel.mockResolvedValue({ chat_session_id: "A", model: "m-strong" });
  await act(async () => {
    root.render(<ChatPage />);
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

function sessionSelect(): HTMLSelectElement {
  return container.querySelector<HTMLSelectElement>('select[title="活会话切换"]')!;
}

function switchTo(select: HTMLSelectElement, value: string) {
  act(() => {
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

describe("批四十一 §7 切走再切回 busy 指示恢复", () => {
  it("切回后 busy 会话显示处理中并禁用输入", async () => {
    await mountWith([SESSION_A, SESSION_B], "A");
    // 初始 activeId = A（busy）→ 输入禁用 + 处理中指示
    const input = container.querySelector<HTMLInputElement>("input[placeholder]")!;
    expect(input.disabled).toBe(true);
    expect(container.textContent).toContain("agent 处理中");

    // 切到 B（空闲）→ 输入可用
    const sel = sessionSelect();
    switchTo(sel, "B");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const inputB = container.querySelector<HTMLInputElement>("input[placeholder]")!;
    expect(inputB.disabled).toBe(false);

    // 切回 A（仍 busy）→ 指示恢复
    switchTo(sel, "A");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const inputA = container.querySelector<HTMLInputElement>("input[placeholder]")!;
    expect(inputA.disabled).toBe(true);
    expect(container.textContent).toContain("agent 处理中");
    expect(container.textContent).toContain("正在处理中");
  });
});

describe("批四十一 §23 停止后 busy 校验清理", () => {
  it("点停止 → 本地已停止 → 轮询确认 busy 翻转 → 输入恢复", async () => {
    await mountWith([SESSION_A], "A");
    const input = container.querySelector<HTMLInputElement>("input[placeholder]")!;
    expect(input.disabled).toBe(true);

    const stopBtn = container.querySelector<HTMLButtonElement>('button[aria-label="停止"]')!;
    expect(stopBtn).toBeTruthy();
    await act(async () => {
      stopBtn.click();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    // 本地已停止行出现
    expect(container.textContent).toContain("已停止");

    // 后端收尾完成：注册表 busy 翻转——校验循环下一轮确认并恢复
    apiMock.listChatSessions.mockResolvedValue({
      sessions: [{ ...SESSION_A, busy: false }],
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });

    expect(apiMock.interruptChatSession).toHaveBeenCalledWith("A");
    expect(apiMock.getChatHistory).toHaveBeenCalledWith("A");
    // 输入恢复可用 + 无残留 busy 警告
    const input2 = container.querySelector<HTMLInputElement>("input[placeholder]")!;
    expect(input2.disabled).toBe(false);
    expect(container.textContent).not.toContain("仍显示忙碌");
  });

  it("10s 未翻转 → 可见警告（不静默残留），输入仍可用", async () => {
    // busy 永不翻转
    apiMock.listChatSessions.mockResolvedValue({
      sessions: [{ ...SESSION_A, busy: true }],
    });
    await mountWith([SESSION_A], "A");
    const stopBtn = container.querySelector<HTMLButtonElement>('button[aria-label="停止"]')!;
    await act(async () => {
      stopBtn.click();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    // 一共推进 5 次轮询（2s * 5 = 10s）
    for (let i = 0; i < 4; i++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
    }
    expect(container.textContent).toContain("仍显示忙碌");
    const input = container.querySelector<HTMLInputElement>("input[placeholder]")!;
    expect(input.disabled).toBe(false);
  });
});

describe("批四十一 §8 会话模型选择", () => {
  it("模型下拉选项来自 /api/models，切换调用 setChatSessionModel", async () => {
    await mountWith([SESSION_A, SESSION_B]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const modelSelect = container.querySelector<HTMLSelectElement>('select[aria-label="会话模型"]')!;
    expect(modelSelect).toBeTruthy();
    const options = Array.from(modelSelect.options).map((o) => o.value);
    expect(options).toEqual(["m-fast", "m-strong"]);

    await act(async () => {
      modelSelect.value = "m-strong";
      modelSelect.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(apiMock.setChatSessionModel).toHaveBeenCalledWith("A", "m-strong");
  });

  it("新建会话带上当前选中的模型", async () => {
    await mountWith([SESSION_A, SESSION_B]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const modelSelect = container.querySelector<HTMLSelectElement>('select[aria-label="会话模型"]')!;
    await act(async () => {
      modelSelect.value = "m-strong";
      modelSelect.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const newBtn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.includes("新建会话"))!;
    await act(async () => {
      newBtn.click();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(apiMock.createChatSession).toHaveBeenCalledWith("m-strong");
  });
});

describe("批四十一 §3/§4 渲染：推理折叠 + 步骤序列", () => {
  it("推理默认折叠为一行摘要，点击展开完整过程", async () => {
    const richHistory: ChatHistoryMessage[] = [
      {
        id: 1, role: "assistant", content: "结论",
        reasoning: "第一步：先看拓扑\n第二步：再查状态\n第三步：给出结论",
        tools: [
          { name: "terminal", input_summary: "nvidia-smi", output_summary: "NVIDIA 4090", ok: true, tool_id: "c1" },
        ],
      },
    ];
    await mountWith([{ ...SESSION_A, busy: false }], undefined, () => richHistory);
    // 默认折叠：不显示完整推理文本
    expect(container.textContent).toContain("查看推理过程");
    expect(container.textContent).not.toContain("第二步：再查状态");
    const toggle = Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.includes("查看推理过程"))!;
    await act(async () => {
      toggle.click();
    });
    expect(container.textContent).toContain("第二步：再查状态");
    // 工具行默认折叠，展开后显示输出
    const toolBtn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.includes("terminal"))!;
    await act(async () => {
      toolBtn.click();
    });
    expect(container.textContent).toContain("NVIDIA 4090");
  });

  it("历史工具行渲染为有序步骤带状态标签（完成）", async () => {
    const richHistory: ChatHistoryMessage[] = [
      {
        id: 1, role: "assistant", content: "",
        tools: [
          { name: "terminal", input_summary: "nvidia-smi", output_summary: "NVIDIA 4090", ok: true, tool_id: "c1" },
          { name: "terminal", input_summary: "mysql -e 'select 1'", output_summary: "count=128", ok: false, tool_id: "c2" },
        ],
      },
    ];
    await mountWith([{ ...SESSION_A, busy: false }], undefined, () => richHistory);
    const text = container.textContent ?? "";
    expect(text).toContain("共 2 步");
    expect(text).toContain("完成");
    expect(text).toContain("失败");
    const tools = Array.from(container.querySelectorAll("button")).filter((b) => b.textContent?.includes("terminal"));
    expect(tools.length).toBeGreaterThanOrEqual(2);
  });
});
