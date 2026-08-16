import { describe, expect, it } from "vitest";

import {
  applyChatEvent,
  chatInputDisabled,
  createChatState,
  markApprovalResolved,
  markTurnInterrupted,
  pushUserMessage,
  stateFromHistory,
  toggleToolExpanded,
} from "./chat";

const ev = (type: string, data: Record<string, unknown> = {}) => ({ type, data });

describe("chat 流式渲染状态机（批三十一）", () => {
  it("chat:delta 增量归并进同一个 assistant 消息", () => {
    let s = createChatState();
    s = applyChatEvent(s, ev("chat:delta", { text: "Hello" }));
    s = applyChatEvent(s, ev("chat:delta", { text: " world" }));
    expect(s.messages).toHaveLength(1);
    expect(s.messages[0].role).toBe("assistant");
    expect(s.messages[0].content).toBe("Hello world");
    expect(s.messages[0].streaming).toBe(true);
  });

  it("chat:done 用 final_response 收口并解除 busy", () => {
    let s = createChatState();
    s = applyChatEvent(s, ev("chat:delta", { text: "partial" }));
    s = applyChatEvent(s, ev("chat:done", { final_response: "final answer" }));
    expect(s.messages[0].content).toBe("final answer");
    expect(s.messages[0].streaming).toBe(false);
    expect(chatInputDisabled(s)).toBe(false);
  });

  it("chat:tool / chat:tool_result 生成工具折叠行并回填摘要", () => {
    let s = createChatState();
    s = applyChatEvent(s, ev("chat:tool", { name: "terminal", input_summary: "kubectl get nodes" }));
    s = applyChatEvent(s, ev("chat:tool_result", { name: "terminal", output_summary: "node1 Ready", ok: true }));
    const msg = s.messages[0];
    expect(msg.tools).toHaveLength(1);
    expect(msg.tools[0].name).toBe("terminal");
    expect(msg.tools[0].inputSummary).toBe("kubectl get nodes");
    expect(msg.tools[0].outputSummary).toBe("node1 Ready");
    expect(msg.tools[0].ok).toBe(true);
  });

  it("tool 折叠切换", () => {
    let s = createChatState();
    s = applyChatEvent(s, ev("chat:tool", { name: "read_file", input_summary: "/etc/hosts" }));
    const toolId = s.messages[0].tools[0].id;
    s = toggleToolExpanded(s, toolId);
    expect(s.messages[0].tools[0].expanded).toBe(true);
    s = toggleToolExpanded(s, toolId);
    expect(s.messages[0].tools[0].expanded).toBe(false);
  });

  it("chat:approval_pending 生成审批卡，markApprovalResolved 更新状态", () => {
    let s = createChatState();
    s = applyChatEvent(s, ev("chat:approval_pending", {
      approval_id: "apv_1",
      command: "kubectl -n prod rollout restart deploy/x",
      env: "prod",
      grade: "L3",
    }));
    expect(s.messages[0].approvals).toHaveLength(1);
    expect(s.messages[0].approvals[0].status).toBe("pending");
    s = markApprovalResolved(s, "apv_1", "approved");
    expect(s.messages[0].approvals[0].status).toBe("approved");
  });

  it("pushUserMessage 追加用户气泡并置 busy（输入禁用）", () => {
    let s = createChatState();
    s = pushUserMessage(s, "看下拓扑");
    expect(s.messages).toHaveLength(1);
    expect(s.messages[0].role).toBe("user");
    expect(s.messages[0].content).toBe("看下拓扑");
    expect(chatInputDisabled(s)).toBe(true);
  });

  it("chat:error 标记消息错误并解除 busy", () => {
    let s = createChatState();
    s = pushUserMessage(s, "hi");
    s = applyChatEvent(s, ev("chat:error", { message: "provider timeout" }));
    expect(s.messages[0].role).toBe("user");
    expect(s.messages[1].error).toBe("provider timeout");
    expect(chatInputDisabled(s)).toBe(false);
  });

  it("无 active 消息时 delta 自动创建 assistant 气泡", () => {
    let s = createChatState();
    s = pushUserMessage(s, "hi");
    s = applyChatEvent(s, ev("chat:delta", { text: "answer" }));
    expect(s.messages).toHaveLength(2);
    expect(s.messages[1].content).toBe("answer");
  });
});

import type { ChatHistoryMessage } from "./api";

describe("批三十三 历史恢复（stateFromHistory）", () => {
  it("历史消息转会话状态：role/content/tools/稳定 key", () => {
    const history: ChatHistoryMessage[] = [
      { id: 101, role: "user", content: "看下拓扑", tools: [], timestamp: 100 },
      {
        id: 102,
        role: "assistant",
        content: "",
        tools: [
          { name: "terminal", input_summary: "kubectl get nodes", output_summary: "node1 Ready", ok: true },
        ],
        timestamp: 101,
      },
      { id: 103, role: "assistant", content: "共 2 台主机。", tools: [], timestamp: 102 },
    ];
    const s = stateFromHistory(history, false);
    expect(s.messages).toHaveLength(3);
    expect(s.messages[0].role).toBe("user");
    expect(s.messages[0].content).toBe("看下拓扑");
    expect(s.messages[1].tools).toHaveLength(1);
    expect(s.messages[1].tools[0].name).toBe("terminal");
    expect(s.messages[1].tools[0].inputSummary).toBe("kubectl get nodes");
    expect(s.messages[1].tools[0].outputSummary).toBe("node1 Ready");
    expect(s.messages[1].tools[0].expanded).toBe(false);
    expect(s.messages[2].content).toBe("共 2 台主机。");
    expect(s.busy).toBe(false);
    // 本地 id 不与服务器行 id 冲突（重编号自 1）
    expect(s.messages[0].id).toBe(1);
    expect(s.messages[1].tools[0].id).toBe(3);
    expect(chatInputDisabled(s)).toBe(false);
  });

  it("busy 历史状态 → 输入禁用（在跑的显示处理中）", () => {
    const history: ChatHistoryMessage[] = [{ id: 1, role: "user", content: "hi", tools: [], timestamp: 1 }];
    const s = stateFromHistory(history, true);
    expect(s.busy).toBe(true);
    expect(chatInputDisabled(s)).toBe(true);
  });

  it("空历史 → 空会话状态", () => {
    const s = stateFromHistory([], false);
    expect(s.messages).toHaveLength(0);
    expect(s.nextId).toBe(1);
  });

  it("无 output_summary 的工具行 → outputSummary undefined 不渲染空输出", () => {
    const history: ChatHistoryMessage[] = [
      { id: 1, role: "assistant", content: "", tools: [{ name: "read_file", input_summary: "/etc/hosts", output_summary: null, ok: null }] },
    ];
    const s = stateFromHistory(history, false);
    expect(s.messages[0].tools[0].outputSummary).toBeUndefined();
  });
});

describe("停止（批三十六 markTurnInterrupted）", () => {
  it("busy 中停止 → busy 解除 + 消息区出现已停止状态行，可继续发新消息", () => {
    let s = createChatState();
    s = pushUserMessage(s, "分析下拓扑");
    s = applyChatEvent(s, ev("chat:delta", { text: "正在分析" }));
    expect(chatInputDisabled(s)).toBe(true);
    s = markTurnInterrupted(s);
    expect(chatInputDisabled(s)).toBe(false);
    const last = s.messages[s.messages.length - 1];
    expect(last.interrupted).toBe(true);
    expect(last.role).toBe("assistant");
    expect(last.streaming).toBe(false);
  });

  it("停止后推入新消息：已停止行保留在上方，新用户气泡追加", () => {
    let s = createChatState();
    s = pushUserMessage(s, "任务 A");
    s = markTurnInterrupted(s);
    s = pushUserMessage(s, "任务 B");
    expect(s.messages).toHaveLength(3);
    expect(s.messages[1].interrupted).toBe(true);
    expect(s.messages[2].role).toBe("user");
    expect(s.messages[2].content).toBe("任务 B");
  });

  it("interrupted 行不进入历史恢复（本地瞬态标记）", () => {
    const s = stateFromHistory([], false);
    expect(s.messages.some((m) => m.interrupted)).toBe(false);
  });
});
