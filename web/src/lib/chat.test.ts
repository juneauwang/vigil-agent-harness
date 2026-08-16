import { describe, expect, it } from "vitest";

import {
  applyChatEvent,
  chatInputDisabled,
  createChatState,
  markApprovalResolved,
  pushUserMessage,
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
