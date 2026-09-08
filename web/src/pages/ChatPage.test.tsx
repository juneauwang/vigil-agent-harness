// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation } from "react-router";
import ChatPage from "./ChatPage";
import { api, ApiError } from "@/lib/api";
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
      answerChatClarify: vi.fn(),
      interruptChatSession: vi.fn(),
      setChatSessionModel: vi.fn(),
      getChatUsage: vi.fn(),
      getUsageAnalytics: vi.fn(),
      approveApproval: vi.fn(),
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
    { id: "m-fast", name: "m-fast", description: "Anthropic Claude Haiku", tag: "快/省", default: true, provider: "openrouter" },
    { id: "m-strong", name: "m-strong", description: "Anthropic Claude Opus", tag: "强/慢", default: false, provider: "openrouter" },
    { id: "internal-v2", name: "internal-v2", description: "公司内部 LLM", tag: "", default: false, provider: "custom:company-internal" },
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
  answerChatClarify: ReturnType<typeof vi.fn>;
  interruptChatSession: ReturnType<typeof vi.fn>;
  setChatSessionModel: ReturnType<typeof vi.fn>;
  getChatUsage: ReturnType<typeof vi.fn>;
  getUsageAnalytics: ReturnType<typeof vi.fn>;
  approveApproval: ReturnType<typeof vi.fn>;
};

if (typeof globalThis.HTMLElement !== "undefined" && !HTMLElement.prototype.scrollIntoView) {
  HTMLElement.prototype.scrollIntoView = () => {};
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
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
  initialEntry = "/chat",
  onLocation?: (search: string) => void,
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
  apiMock.setChatSessionModel.mockImplementation(async (_sid: string, model: string) => ({
    chat_session_id: "A",
    model,
  }));
  apiMock.getChatUsage.mockResolvedValue({
    ok: true,
    session_id: "A",
    model: "deepseek-v4-flash",
    input_tokens: 15248,
    output_tokens: 76,
    total_tokens: 15324,
    cost: 0.002156,
    cost_currency: "usd",
    price_source: "builtin",
    price: { input_per_1m: 0.14, output_per_1m: 0.28, currency: "usd", source: "builtin" },
  });
  apiMock.getUsageAnalytics.mockResolvedValue({
    daily: [{ day: "2026-08-23", input_tokens: 5000, output_tokens: 100, cache_read_tokens: 0, reasoning_tokens: 0, estimated_cost: 0.01, actual_cost: 0, sessions: 1, api_calls: 1 }],
    totals: { total_input: 10000, total_output: 200, total_cache_read: 0, total_reasoning: 0, total_estimated_cost: 0.03, total_actual_cost: 0, total_sessions: 2, total_api_calls: 3 },
    period_days: 30,
  });
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[initialEntry]}>
        {onLocation && <LocationProbe onLocation={onLocation} />}
        <ChatPage />
      </MemoryRouter>,
    );
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

function LocationProbe({ onLocation }: { onLocation: (search: string) => void }) {
  const loc = useLocation();
  onLocation(`${loc.pathname}${loc.search}`);
  return null;
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

async function openUsagePanel() {
  const btn = [...container.querySelectorAll("button")].find((b) => b.textContent?.includes("用量"))!;
  await act(async () => {
    btn.click();
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

/** 填输入框 + 提交表单（React 受控 input 需走原生 setter）。 */
async function sendMessage(text: string) {
  const input = container.querySelector<HTMLInputElement>("input[placeholder]")!;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
  await act(async () => {
    setter.call(input, text);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  const form = input.closest("form")!;
  await act(async () => {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

describe("批四十一 §7 切走再切回 busy 指示恢复", () => {
  it("切回后 busy 会话显示处理中指示；输入保持可用（task31 PART C type-to-interrupt），placeholder 提示将中断", async () => {
    await mountWith([SESSION_A, SESSION_B], "A");
    // 初始 activeId = A（busy）→ 输入可用 + 处理中指示 + 中断提示 placeholder
    const input = container.querySelector<HTMLInputElement>("input[placeholder]")!;
    expect(input.disabled).toBe(false);
    expect(input.placeholder).toBe("输入将中断当前任务");
    expect(container.textContent).toContain("agent 处理中");

    // 切到 B（空闲）→ 输入可用，placeholder 恢复常规
    const sel = sessionSelect();
    switchTo(sel, "B");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const inputB = container.querySelector<HTMLInputElement>("input[placeholder]")!;
    expect(inputB.disabled).toBe(false);
    expect(inputB.placeholder).not.toBe("输入将中断当前任务");

    // 切回 A（仍 busy）→ 指示恢复
    switchTo(sel, "A");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const inputA = container.querySelector<HTMLInputElement>("input[placeholder]")!;
    // task31 PART C：busy 不再禁输入，但中断提示与 busy 指示恢复
    expect(inputA.disabled).toBe(false);
    expect(inputA.placeholder).toBe("输入将中断当前任务");
    expect(container.textContent).toContain("agent 处理中");
    expect(container.textContent).toContain("正在处理中");
  });
});

describe("批四十一 §23 停止后 busy 校验清理", () => {
  it("点停止 → 本地已停止 → 轮询确认 busy 翻转 → 输入恢复", async () => {
    await mountWith([SESSION_A], "A");
    const input = container.querySelector<HTMLInputElement>("input[placeholder]")!;
    // task31 PART C：busy 下输入本就可用；恢复信号 = placeholder 离开中断提示
    expect(input.disabled).toBe(false);
    expect(input.placeholder).toBe("输入将中断当前任务");

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
    // 输入恢复常规 placeholder + 无残留 busy 警告
    const input2 = container.querySelector<HTMLInputElement>("input[placeholder]")!;
    expect(input2.disabled).toBe(false);
    expect(input2.placeholder).not.toBe("输入将中断当前任务");
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

describe("批四十九 对话流内 clarify 卡", () => {
  it("clarify_pending 事件 → 问题卡出现；选选择 + 提交 → answerChatClarify 收到", async () => {
    let emit: ((e: { type: string; data: unknown }) => void) | null = null;
    let resolveStream: (() => void) | null = null;
    apiMock.answerChatClarify.mockResolvedValue({ status: "resolved", clarify_id: "clfy_1" });
    await mountWith([{ ...SESSION_A, busy: false }]);
    apiMock.chatStream.mockImplementation(async (_sid: string, _msg: string, onEvent: (e: { type: string; data: unknown }) => void) => {
      emit = onEvent;
      onEvent({
        type: "chat:clarify_pending",
        data: {
          clarify_id: "clfy_1",
          question: "选哪个部署目标？",
          choices: ["staging", "prod"],
          multi_select: false,
          timeout_at: "2099-01-01T00:00:00Z",
        },
      });
      await new Promise<void>((r) => {
        resolveStream = r;
      });
    });
    await sendMessage("继续部署");

    expect(container.textContent).toContain("选哪个部署目标？");
    expect(container.textContent).toContain("staging");
    expect(container.textContent).toContain("prod");
    const stagingBtn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.trim() === "staging")!;
    expect(stagingBtn).toBeTruthy();
    await act(async () => {
      stagingBtn.click();
    });
    const submitBtn = container.querySelector<HTMLButtonElement>('button[data-testid="clarify-submit-clfy_1"]')!;
    expect(submitBtn).toBeTruthy();
    await act(async () => {
      submitBtn.click();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(apiMock.answerChatClarify).toHaveBeenCalledWith("A", "staging");
    // 回合收尾：卡片仍显示已提交（done 收口不覆盖已答状态）。
    await act(async () => {
      emit!({ type: "chat:done", data: { final_response: "好的，用 staging。" } });
      resolveStream!();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(container.textContent).toContain("已提交，agent 继续");
  });

  it("已超时的 clarify 卡显示『已超时，agent 自行决定』", async () => {
    await mountWith([{ ...SESSION_A, busy: false }]);
    apiMock.chatStream.mockImplementation(async (_sid: string, _msg: string, onEvent: (e: { type: string; data: unknown }) => void) => {
      onEvent({
        type: "chat:clarify_pending",
        data: {
          clarify_id: "clfy_tmo",
          question: "密码确认？",
          choices: null,
          multi_select: false,
          timeout_at: "2020-01-01T00:00:00Z",
        },
      });
      onEvent({ type: "chat:done", data: { final_response: "我自己决定了。" } });
    });
    await sendMessage("继续");
    expect(container.textContent).toContain("密码确认？");
    expect(container.textContent).toContain("已超时，agent 自行决定");
    // 超时后提交按钮不再渲染（不可再答）。
    expect(container.querySelector('button[data-testid="clarify-submit-clfy_tmo"]')).toBeNull();
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
    expect(options).toEqual(["m-fast", "m-strong", "internal-v2"]);
    // 批五十一：下拉按 provider 分组（optgroup）。
    const groups = Array.from(modelSelect.querySelectorAll("optgroup")).map((g) => g.label);
    expect(groups).toEqual(["openrouter", "custom:company-internal"]);
    // 批四十二 §AY：下拉只显示模型名 + 默认标识，不渲染用途 tag。
    const optionTexts = Array.from(modelSelect.options).map((o) => o.textContent ?? "");
    expect(optionTexts.join("|")).not.toContain("快/省");
    expect(optionTexts.join("|")).not.toContain("强/慢");
    expect(optionTexts[0]).toContain("默认");
    expect(optionTexts[1]).not.toContain("默认");

    await act(async () => {
      modelSelect.value = "m-strong";
      modelSelect.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(apiMock.setChatSessionModel).toHaveBeenCalledWith("A", "m-strong", "openrouter");

    // 切到 custom provider 模型 → provider 随提交。
    await act(async () => {
      modelSelect.value = "internal-v2";
      modelSelect.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(apiMock.setChatSessionModel).toHaveBeenCalledWith("A", "internal-v2", "custom:company-internal");
  });

  it("新建会话带上当前选中的模型", async () => {
    await mountWith([SESSION_A, SESSION_B]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const modelSelect = container.querySelector<HTMLSelectElement>('select[aria-label="会话模型"]')!;
    await act(async () => {
      modelSelect.value = "internal-v2";
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
    expect(apiMock.createChatSession).toHaveBeenCalledWith("internal-v2", "custom:company-internal");
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

describe("批四十二 §BI 结论渲染顺序", () => {
  it("结论内容渲染在步骤序列之后（DOM 顺序），历史消息同样生效", async () => {
    const richHistory: ChatHistoryMessage[] = [
      {
        id: 1, role: "assistant", content: "结论：Runbook 已成功创建",
        tools: [
          { name: "terminal", input_summary: "nvidia-smi", output_summary: "NVIDIA 4090", ok: true, tool_id: "c1" },
        ],
      },
    ];
    await mountWith([{ ...SESSION_A, busy: false }], undefined, () => richHistory);
    const stepList = container.querySelector<HTMLElement>('[data-testid="step-list"]')!;
    const content = container.querySelector<HTMLElement>('[data-testid="assistant-content"]')!;
    expect(stepList).toBeTruthy();
    expect(content).toBeTruthy();
    expect(
      stepList.compareDocumentPosition(content) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("结论为空时（流式/无内容）不渲染空占位，步骤序列仍在", async () => {
    const richHistory: ChatHistoryMessage[] = [
      {
        id: 1, role: "assistant", content: "",
        tools: [
          { name: "terminal", input_summary: "nvidia-smi", output_summary: "NVIDIA 4090", ok: true, tool_id: "c1" },
        ],
      },
    ];
    await mountWith([{ ...SESSION_A, busy: false }], undefined, () => richHistory);
    expect(container.querySelector('[data-testid="assistant-content"]')).toBeNull();
    expect(container.querySelector('[data-testid="step-list"]')).toBeTruthy();
  });
});

describe("批四十二 §BJ/§BK reasoning 按工具步挂载渲染", () => {
  it("历史结构化推理挂回工具行：'该步推理'默认折叠，点击展开", async () => {
    const richHistory: ChatHistoryMessage[] = [
      {
        id: 1, role: "assistant", content: "完成",
        reasoning: { steps: [{ tool_id: "c1", text: "第一步推理：先看拓扑\n第二步：再看状态" }] },
        tools: [
          { name: "terminal", input_summary: "nvidia-smi", output_summary: "NVIDIA 4090", ok: true, tool_id: "c1" },
        ],
      },
    ];
    await mountWith([{ ...SESSION_A, busy: false }], undefined, () => richHistory);
    expect(container.textContent).toContain("该步推理");
    // 默认折叠：只显示首行摘要，不显示后续内容
    expect(container.textContent).not.toContain("第二步：再看状态");
    const reasonBtn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.includes("该步推理"))!;
    await act(async () => {
      reasonBtn.click();
    });
    expect(container.textContent).toContain("第二步：再看状态");
  });

  it("审批卡显示触发步骤的推理摘要（可展开），默认折叠", async () => {
    await mountWith([{ ...SESSION_A, busy: false }]);
    apiMock.chatStream.mockImplementation(async (_sid, _msg, onEvent) => {
      onEvent({ type: "chat:tool", data: { tool_id: "call_1", name: "terminal", input_summary: "kubectl delete pod x" } });
      onEvent({ type: "chat:reasoning", data: { tool_id: "call_1", text: "先确认删除 pod x 的影响范围\n再执行删除" } });
      onEvent({ type: "chat:approval_pending", data: { approval_id: "apv_1", command: "kubectl delete pod x", env: "prod" } });
      onEvent({ type: "chat:done", data: { final_response: "已删除" } });
    });
    await sendMessage("删除 pod");
    expect(container.textContent).toContain("触发推理");
    // 默认折叠：摘要只显示首行，第二行内容不可见
    expect(container.textContent).not.toContain("再执行删除");
    const reasonBtn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.includes("触发推理"))!;
    await act(async () => {
      reasonBtn.click();
    });
    expect(container.textContent).toContain("再执行删除");
  });
});

describe("批四十二 §BH 全局弹窗批准 → 对话内审批卡同步", () => {
  it("全局批准广播后，对话内对应审批卡立即变已批准", async () => {
    await mountWith([{ ...SESSION_A, busy: false }]);
    apiMock.chatStream.mockImplementation(async (_sid, _msg, onEvent) => {
      onEvent({ type: "chat:tool", data: { tool_id: "call_1", name: "terminal", input_summary: "kubectl delete pod x" } });
      onEvent({ type: "chat:approval_pending", data: { approval_id: "apv_1", command: "kubectl delete pod x", env: "prod" } });
      onEvent({ type: "chat:done", data: { final_response: "已删除" } });
    });
    await sendMessage("删除 pod");
    expect(container.textContent).toContain("等待审批");

    // 模拟全局审批弹窗批准成功后的广播（ApprovalModal resolve → notify）。
    const { notifyApprovalResolved } = await import("@/lib/approvalEvents");
    await act(async () => {
      notifyApprovalResolved("apv_1", "approved");
    });
    expect(container.textContent).toContain("已批准");
    expect(container.textContent).not.toContain("等待审批");
  });

  it("全局拒绝广播后，对话内对应审批卡立即变已拒绝", async () => {
    await mountWith([{ ...SESSION_A, busy: false }]);
    apiMock.chatStream.mockImplementation(async (_sid, _msg, onEvent) => {
      onEvent({ type: "chat:tool", data: { tool_id: "call_1", name: "terminal", input_summary: "rm -rf x" } });
      onEvent({ type: "chat:approval_pending", data: { approval_id: "apv_2", command: "rm -rf x", env: "test" } });
      onEvent({ type: "chat:done", data: { final_response: "已处理" } });
    });
    await sendMessage("清理目录");
    expect(container.textContent).toContain("等待审批");
    const { notifyApprovalResolved } = await import("@/lib/approvalEvents");
    await act(async () => {
      notifyApprovalResolved("apv_2", "denied");
    });
    expect(container.textContent).toContain("已拒绝");
    expect(container.textContent).not.toContain("等待审批");
  });
});

describe("待审批浮层（BUGFIX OPS-DELTA #107）", () => {
  it("pending 审批卡同时出现在消息流底部浮层；批准后浮层消失", async () => {
    await mountWith([{ ...SESSION_A, busy: false }]);
    apiMock.approveApproval.mockResolvedValue({ ok: true } as never);
    apiMock.chatStream.mockImplementation(async (_sid, _msg, onEvent) => {
      onEvent({ type: "chat:tool", data: { tool_id: "call_1", name: "terminal", input_summary: "kubectl delete pod x" } });
      onEvent({ type: "chat:approval_pending", data: { approval_id: "apv_9", command: "kubectl delete pod x", env: "prod" } });
      // no chat:done — approval stays pending, card must float at stream bottom
    });
    await sendMessage("删除 pod");
    const float = container.querySelector('[data-testid="pending-cards-float"]');
    expect(float).not.toBeNull();
    expect(float!.textContent).toContain("等待审批");
    // Resolve via the floating card's approve button.
    const approveBtn = Array.from(float!.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("批准"),
    )!;
    await act(async () => {
      approveBtn.click();
    });
    expect(apiMock.approveApproval).toHaveBeenCalledWith("apv_9", "once");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(container.querySelector('[data-testid="pending-cards-float"]')).toBeNull();
  });
});

describe("批六十四 chat 用量面板", () => {
  it("打开面板 → 拉当前会话实时 token + 历史累计，显示 token 与费用", async () => {
    await mountWith([SESSION_A]);
    expect(container.querySelector('[data-testid="usage-panel"]')).toBeNull();
    await openUsagePanel();
    const panel = container.querySelector('[data-testid="usage-panel"]')!;
    expect(apiMock.getChatUsage).toHaveBeenCalledWith("A");
    expect(apiMock.getUsageAnalytics).toHaveBeenCalledWith(30);
    // 当前会话：模型 + token + 费用（内置估算标签）
    expect(panel.textContent).toContain("当前会话");
    expect(panel.textContent).toContain("deepseek-v4-flash");
    expect(panel.textContent).toContain("15,248");
    expect(panel.textContent).toContain("76");
    expect(panel.textContent).toContain("15,324");
    expect(panel.textContent).toContain("约 $0.00");
    expect(panel.textContent).toContain("内置估算");
    // 今天 / 近 30 天（历史累计，含全部会话）
    expect(panel.textContent).toContain("今天");
    expect(panel.textContent).toContain("5,000");
    expect(panel.textContent).toContain("近 30 天");
    expect(panel.textContent).toContain("10,000");
    expect(panel.textContent).toContain("约 $0.03");
  });

  it("价格不可用 → 只显 token，不显费用（不瞎算）", async () => {
    await mountWith([SESSION_A]);
    apiMock.getChatUsage.mockResolvedValue({
      ok: true,
      session_id: "A",
      model: "unknown-model",
      input_tokens: 500,
      output_tokens: 100,
      total_tokens: 600,
      cost: null,
      cost_currency: null,
      price_source: null,
      price: null,
    });
    await openUsagePanel();
    const panel = container.querySelector('[data-testid="usage-panel"]')!;
    expect(panel.textContent).toContain("价格不可用，仅显示 token");
    expect(panel.textContent).toContain("500");
    expect(panel.textContent).toContain("600");
    // 当前会话卡片内无费用行（今天/近 30 天来自 analytics 记录的
    // estimated_cost，属历史累计，不受当前会话价格影响）。
    const currentCard = panel.querySelector(".grid > div")!;
    expect(currentCard.textContent).not.toContain("约 $");
  });

  it("无会话（创建失败，activeId 空）→ 空态提示", async () => {
    apiMock.getModels.mockResolvedValue(MODELS);
    apiMock.listChatSessions.mockResolvedValue({ sessions: [], total: 0 });
    apiMock.createChatSession.mockRejectedValue(new Error("backend down"));
    apiMock.getUsageAnalytics.mockResolvedValue({ daily: [], totals: {}, period_days: 30 });
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/chat"]}>
          <ChatPage />
        </MemoryRouter>,
      );
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await openUsagePanel();
    const panel = container.querySelector('[data-testid="usage-panel"]')!;
    expect(panel.textContent).toContain("创建会话后显示用量");
    expect(apiMock.getChatUsage).not.toHaveBeenCalled();
  });

  it("切换会话 → 按新会话重拉 usage", async () => {
    await mountWith([SESSION_A, SESSION_B]);
    await openUsagePanel();
    expect(apiMock.getChatUsage).toHaveBeenCalledWith("A");
    apiMock.getChatUsage.mockClear();
    switchTo(sessionSelect(), "B");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(apiMock.getChatUsage).toHaveBeenCalledWith("B");
  });

  it("URL sid 存在且在列表内 → 刷新回到该会话", async () => {
    await mountWith([SESSION_A, SESSION_B], undefined, undefined, "/chat?sid=B");
    // 初始 activeId = B（不是列表首个 A）：B 空闲 → 输入可用。
    const input = container.querySelector<HTMLInputElement>("input[placeholder]")!;
    expect(input.disabled).toBe(false);
    const sel = sessionSelect();
    expect(sel.value).toBe("B");
  });

  it("URL sid 无效/已清 → 静默回退列表首个，不报错", async () => {
    await mountWith([SESSION_A, SESSION_B], undefined, undefined, "/chat?sid=GONE");
    const sel = sessionSelect();
    expect(sel.value).toBe("A");
    expect(container.textContent).not.toContain("error");
  });
});

// ---------------------------------------------------------------------------
// task19 F2 — Runbooks 缺口 → chat 桥：?prompt= 深链预填（镜像 ?sid= 的 replace 清理）
// ---------------------------------------------------------------------------

describe("ChatPage ?prompt= prefill (task19 F2)", () => {
  it("mount 时预填草稿并清除 prompt 参数", async () => {
    let search = "";
    const prompt = "帮 reboot (5 uses) 写一个 runbook";
    await mountWith(
      [SESSION_B],
      undefined,
      undefined,
      `/chat?prompt=${encodeURIComponent(prompt)}`,
      (s) => {
        search = s;
      },
    );
    const input = container.querySelector<HTMLInputElement>("input[placeholder]")!;
    expect(input.value).toBe(prompt);
    // 参数用完即清（replace，不污染历史），?sid= 语义不受影响
    expect(search).toBe("/chat");
  });

  it("busy 会话挂载时不预填（静默忽略，参数同样清除）", async () => {
    let search = "";
    await mountWith(
      [SESSION_A],
      "A",
      undefined,
      `/chat?prompt=${encodeURIComponent("busy case prompt")}`,
      (s) => {
        search = s;
      },
    );
    const inputs = Array.from(
      container.querySelectorAll<HTMLInputElement>("input[placeholder]"),
    );
    expect(inputs.some((i) => i.value === "busy case prompt")).toBe(false);
    expect(search).toBe("/chat");
  });
});

// ── task31 PART B：长回合停止可靠性（根因：渲染谓词 local‖registry vs 守卫只看 local）──
describe("ChatPage 停止可靠性（task31 PART B）", () => {
  it("长回合：poll 用 busy:false 重建本槽后（desync），停止按钮仍真正发出中断", async () => {
    await mountWith([SESSION_A, SESSION_B], "A");
    expect(apiMock.interruptChatSession).not.toHaveBeenCalled();

    // poll tick（1s）：busy 分支用 stateFromHistory(…, false) 重建本槽 →
    // 本槽 busy 变 false，busyMap 保持 true（OPS-DELTA #107 的刻意设计）。
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });

    // desync 生效：本槽不再 disabled，但停止按钮仍在（effective busy）。
    const stopBtn = [...container.querySelectorAll("button")].find(
      (b) => b.getAttribute("aria-label") === "停止",
    )!;
    expect(stopBtn).toBeTruthy();

    await act(async () => {
      stopBtn.click();
      await vi.advanceTimersByTimeAsync(0);
    });
    // 修复前：守卫只看本槽 busy → 点击被静默吞掉（interrupt 不发）。
    expect(apiMock.interruptChatSession).toHaveBeenCalledTimes(1);
    expect(apiMock.interruptChatSession).toHaveBeenCalledWith("A");
    // 本地立即复位：出现"已停止"标记行，输入恢复可用。
    expect(container.textContent).toContain("已停止");
  });

  it("interrupt 请求失败 → 重试一次；仍失败 → 错误上屏 + 本地强制复位（无需刷新）", async () => {
    await mountWith([SESSION_A, SESSION_B], "A");
    apiMock.interruptChatSession.mockRejectedValue(
      new ApiError("internal", "upstream exploded", 500),
    );
    const stopBtn = [...container.querySelectorAll("button")].find(
      (b) => b.getAttribute("aria-label") === "停止",
    )!;
    await act(async () => {
      stopBtn.click();
      await vi.advanceTimersByTimeAsync(0);
    });
    // 第一次失败 → 800ms 后重试
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(apiMock.interruptChatSession).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    // 错误上屏（不静默）；本地照样复位（已停止标记 + 输入不再锁死）
    expect(container.textContent).toContain("停止请求未送达");
    expect(container.textContent).toContain("已停止");
  });

  it("interrupt 409 not_busy（会话已收敛）→ 视为成功：不重试、不报错，本地复位", async () => {
    await mountWith([SESSION_A, SESSION_B], "A");
    apiMock.interruptChatSession.mockRejectedValue(
      new ApiError("not_busy", "会话当前没有进行中的操作", 409),
    );
    const stopBtn = [...container.querySelectorAll("button")].find(
      (b) => b.getAttribute("aria-label") === "停止",
    )!;
    await act(async () => {
      stopBtn.click();
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(apiMock.interruptChatSession).toHaveBeenCalledTimes(1); // 409 不重试
    expect(container.textContent).not.toContain("停止请求未送达");
    expect(container.textContent).toContain("已停止");
  });
});

// ── task31 PART C：type-to-interrupt（busy 提交 = 打断 + 同会话续发）──
describe("ChatPage type-to-interrupt（task31 PART C）", () => {
  async function mountBusy() {
    await mountWith([SESSION_A, SESSION_B], "A");
    const input = container.querySelector<HTMLInputElement>("input[placeholder]")!;
    expect(input.disabled).toBe(false); // busy 不再禁输入
    return input;
  }

  it("busy 提交 → 走共享取消路径打断 + 同会话立即发新消息，草稿清空", async () => {
    const input = await mountBusy();
    await sendMessage("新指令，打断一下");
    // 共享取消路径：中断发到同一会话
    expect(apiMock.interruptChatSession).toHaveBeenCalledWith("A");
    // 新消息作为同一会话的下一 turn 立即发出
    expect(apiMock.chatStream).toHaveBeenCalledWith(
      "A",
      "新指令，打断一下",
      expect.any(Function),
      expect.anything(),
    );
    // 草稿清空 + 用户消息进时间线
    expect(input.value).toBe("");
    expect(container.textContent).toContain("新指令，打断一下");
    expect(container.textContent).toContain("已停止"); // 被打断回合带中断标记
  });

  it("打断收尾竞态：首个 chatStream 409 busy → 就地重试成功，消息不丢", async () => {
    await mountBusy();
    apiMock.chatStream
      .mockRejectedValueOnce(new ApiError("busy", "会话正在处理", 409))
      .mockResolvedValueOnce(undefined);
    await sendMessage("竞态消息");
    // 409 → drain 等 1s 就地重试（fake timers 推进）
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    const calls = apiMock.chatStream.mock.calls.filter(
      (c) => c[1] === "竞态消息",
    );
    expect(calls.length).toBe(2); // 首发失败 + 重试成功
    expect(container.textContent).not.toContain("已放回输入框");
  });

  it("空闲会话提交不走中断路径（行为不变）", async () => {
    await mountWith([SESSION_A, SESSION_B], "A");
    const sel = sessionSelect();
    switchTo(sel, "B");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await sendMessage("正常提问");
    expect(apiMock.interruptChatSession).not.toHaveBeenCalled();
    expect(apiMock.chatStream).toHaveBeenCalledWith(
      "B",
      "正常提问",
      expect.any(Function),
      expect.anything(),
    );
  });
});

// ── task32 PART A：审批到点后卡片离开底部浮层（不钉死），终态留在消息流 ──
describe("审批超时卡片收敛（task32 PART A）", () => {
  it("到点未决卡：浮层消失 + 消息流内显示已超时终态（无批准按钮）", async () => {
    await mountWith([{ ...SESSION_A, busy: false }]);
    const pastTimeout = new Date(Date.now() - 60_000).toISOString();
    apiMock.chatStream.mockImplementation(async (_sid, _msg, onEvent) => {
      onEvent({ type: "chat:tool", data: { tool_id: "call_1", name: "terminal", input_summary: "kubectl delete pod x" } });
      onEvent({
        type: "chat:approval_pending",
        data: { approval_id: "apv_t", command: "kubectl delete pod x", env: "prod", timeout_at: pastTimeout },
      });
      // no chat:done — 后端审批到点终止，turn 尚未收尾的窗口内卡片应离开浮层
    });
    await sendMessage("删除 pod");

    // sweep tick（1s）跑完：卡片收敛为 timed_out
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });

    // 浮层放行（不再钉在底部）
    expect(container.querySelector('[data-testid="pending-cards-float"]')).toBeNull();
    // 消息流内的卡片保留历史事实：已超时终态 + 无批准/拒绝按钮（死审批不可点）
    expect(container.textContent).toContain("审批超时，已终止");
    const cards = [...container.querySelectorAll("div")].filter((d) =>
      d.textContent?.includes("kubectl delete pod x"),
    );
    const cardRoot = cards[cards.length - 1]!;
    expect([...cardRoot.querySelectorAll("button")].some((b) => b.textContent?.includes("批准"))).toBe(false);
    expect([...cardRoot.querySelectorAll("button")].some((b) => b.textContent?.includes("拒绝"))).toBe(false);
    // 没有向 API 发过死审批请求（客户端拒绝）
    expect(apiMock.approveApproval).not.toHaveBeenCalled();
  });
});
