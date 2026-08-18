import { describe, expect, it } from "vitest";

import {
  approvalIsTimedOut,
  applyChatEvent,
  chatInputDisabled,
  createChatState,
  markApprovalResolved,
  markApprovalResolvedInSessions,
  markTurnInterrupted,
  pushUserMessage,
  stateFromHistory,
  toggleToolExpanded,
  type ChatTurnState,
} from "./chat";

const TIMEOUT_AT = "2026-08-17T12:00:00Z";

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

describe("审批超时展示（批三十八 §AW）", () => {
  it("pending + timeout_at 已过 → 超时", () => {
    expect(approvalIsTimedOut({ status: "pending", timeoutAt: TIMEOUT_AT }, Date.parse(TIMEOUT_AT) + 1)).toBe(true);
  });

  it("pending + timeout_at 未到 → 未超时", () => {
    expect(approvalIsTimedOut({ status: "pending", timeoutAt: TIMEOUT_AT }, Date.parse(TIMEOUT_AT) - 1)).toBe(false);
  });

  it("已批准/已拒绝不再计超时", () => {
    expect(approvalIsTimedOut({ status: "approved", timeoutAt: TIMEOUT_AT }, Date.parse(TIMEOUT_AT) + 1)).toBe(false);
    expect(approvalIsTimedOut({ status: "denied", timeoutAt: TIMEOUT_AT }, Date.parse(TIMEOUT_AT) + 1)).toBe(false);
  });

  it("无 timeout_at 或非法值 → 不算超时", () => {
    expect(approvalIsTimedOut({ status: "pending", timeoutAt: null })).toBe(false);
    expect(approvalIsTimedOut({ status: "pending", timeoutAt: "not-a-date" })).toBe(false);
  });
});

describe("批四十一 §5 工具输出零错位（硬性回归）", () => {
  it("并行同名单工具：结果按服务端 tool_id 挂接，不倒挂", () => {
    let s = createChatState();
    s = applyChatEvent(s, ev("chat:tool", { tool_id: "call_1", name: "terminal", input_summary: "nvidia-smi" }));
    s = applyChatEvent(s, ev("chat:tool", { tool_id: "call_2", name: "terminal", input_summary: "mysql -e 'select count(*)'" }));
    // 结果按完成顺序到达（call_1 先到）——旧实现按"最后一个同名"会挂到
    // call_2 上，这里断言严格按 id 挂接。
    s = applyChatEvent(s, ev("chat:tool_result", { tool_id: "call_1", name: "terminal", output_summary: "NVIDIA 4090", ok: true }));
    s = applyChatEvent(s, ev("chat:tool_result", { tool_id: "call_2", name: "terminal", output_summary: "count=128", ok: true }));
    const [t1, t2] = s.messages[0].tools;
    expect(t1.toolId).toBe("call_1");
    expect(t1.outputSummary).toBe("NVIDIA 4090");
    expect(t2.toolId).toBe("call_2");
    expect(t2.outputSummary).toBe("count=128");
    expect(t1.ok).toBe(true);
    expect(t2.ok).toBe(true);
    // 步骤状态：两个工具都完成
    expect(s.messages[0].steps.map((x) => x.status)).toEqual(["done", "done"]);
  });

  it("异构工具交错结果不串门", () => {
    let s = createChatState();
    s = applyChatEvent(s, ev("chat:tool", { tool_id: "c_a", name: "terminal", input_summary: "ls" }));
    s = applyChatEvent(s, ev("chat:tool", { tool_id: "c_b", name: "read_file", input_summary: "/etc/hosts" }));
    s = applyChatEvent(s, ev("chat:tool_result", { tool_id: "c_a", name: "terminal", output_summary: "file1", ok: true }));
    s = applyChatEvent(s, ev("chat:tool_result", { tool_id: "c_b", name: "read_file", output_summary: "127.0.0.1 localhost", ok: true }));
    const tools = s.messages[0].tools;
    expect(tools[0].name).toBe("terminal");
    expect(tools[0].outputSummary).toBe("file1");
    expect(tools[1].name).toBe("read_file");
    expect(tools[1].outputSummary).toBe("127.0.0.1 localhost");
  });

  it("无 tool_id（旧服务端）：兜底按最后一个同名未出结果挂接", () => {
    let s = createChatState();
    s = applyChatEvent(s, ev("chat:tool", { name: "terminal", input_summary: "a" }));
    s = applyChatEvent(s, ev("chat:tool", { name: "terminal", input_summary: "b" }));
    s = applyChatEvent(s, ev("chat:tool_result", { name: "terminal", output_summary: "out1", ok: true }));
    const tools = s.messages[0].tools;
    expect(tools[1].outputSummary).toBe("out1");
    expect(tools[0].outputSummary).toBeUndefined();
  });

  it("history 恢复：工具行保留 tool_id，步骤按完成态生成", () => {
    const history: ChatHistoryMessage[] = [
      {
        id: 1, role: "assistant", content: "",
        tools: [
          { name: "terminal", input_summary: "nvidia-smi", output_summary: "NVIDIA", ok: true, tool_id: "call_1" },
          { name: "terminal", input_summary: "mysql", output_summary: "rows", ok: false, tool_id: "call_2" },
        ],
      },
    ];
    const s = stateFromHistory(history, false);
    expect(s.messages[0].tools[0].toolId).toBe("call_1");
    expect(s.messages[0].tools[1].toolId).toBe("call_2");
    expect(s.messages[0].steps.map((x) => x.status)).toEqual(["done", "failed"]);
  });
});

describe("批四十一 §3 推理过程", () => {
  it("chat:reasoning 增量归并进同一消息的 reasoning 字段", () => {
    let s = createChatState();
    s = applyChatEvent(s, ev("chat:reasoning", { text: "先查拓扑" }));
    s = applyChatEvent(s, ev("chat:reasoning", { text: "再看状态" }));
    s = applyChatEvent(s, ev("chat:delta", { text: "结论" }));
    s = applyChatEvent(s, ev("chat:done", { final_response: "结论完整版" }));
    expect(s.messages).toHaveLength(1);
    expect(s.messages[0].reasoning).toBe("先查拓扑再看状态");
    expect(s.messages[0].content).toBe("结论完整版");
  });

  it("history 恢复带 reasoning（默认折叠的数据源）", () => {
    const history: ChatHistoryMessage[] = [
      { id: 1, role: "assistant", content: "ok", reasoning: "hidden chain of thought", tools: [] },
    ];
    const s = stateFromHistory(history, false);
    expect(s.messages[0].reasoning).toBe("hidden chain of thought");
  });
});

describe("批四十一 §4 有序步骤序列", () => {
  it("工具+审批按到达顺序串成步骤，状态标签随事件更新", () => {
    let s = createChatState();
    s = applyChatEvent(s, ev("chat:tool", { tool_id: "c1", name: "terminal", input_summary: "kubectl get nodes" }));
    s = applyChatEvent(s, ev("chat:approval_pending", { approval_id: "apv_1", command: "kubectl delete pod x", env: "prod" }));
    s = applyChatEvent(s, ev("chat:approval_pending", { approval_id: "apv_2", command: "kubectl delete pod y", env: "prod" }));
    s = applyChatEvent(s, ev("chat:tool_result", { tool_id: "c1", name: "terminal", output_summary: "node1 Ready", ok: true }));
    const msg = s.messages[0];
    expect(msg.steps.map((x) => x.kind)).toEqual(["tool", "approval", "approval"]);
    expect(msg.steps.map((x) => x.status)).toEqual(["done", "pending", "pending"]);
    s = markApprovalResolved(s, "apv_1", "approved");
    expect(s.messages[0].steps[1].status).toBe("approved");
    s = markApprovalResolved(s, "apv_2", "denied");
    expect(s.messages[0].steps[2].status).toBe("denied");
  });

  it("空消息的 tool/approval 事件自动创建 assistant 气泡并串步", () => {
    let s = createChatState();
    s = applyChatEvent(s, ev("chat:tool", { tool_id: "c1", name: "terminal", input_summary: "ls" }));
    s = applyChatEvent(s, ev("chat:approval_pending", { approval_id: "apv_2", command: "rm -rf x", env: "test" }));
    expect(s.messages).toHaveLength(1);
    expect(s.messages[0].steps.map((x) => x.kind)).toEqual(["tool", "approval"]);
  });
});

describe("批四十二 §BJ reasoning 按工具步挂载", () => {
  it("chat:reasoning 带 tool_id → 归并进对应工具的 reasoning，不进消息级", () => {
    let s = createChatState();
    s = applyChatEvent(s, ev("chat:tool", { tool_id: "call_1", name: "terminal", input_summary: "kubectl get nodes" }));
    s = applyChatEvent(s, ev("chat:reasoning", { tool_id: "call_1", text: "先确认节点状态" }));
    s = applyChatEvent(s, ev("chat:reasoning", { tool_id: "call_1", text: "再看调度" }));
    s = applyChatEvent(s, ev("chat:tool_result", { tool_id: "call_1", name: "terminal", output_summary: "node1 Ready", ok: true }));
    const msg = s.messages[0];
    expect(msg.tools[0].reasoning).toBe("先确认节点状态再看调度");
    expect(msg.reasoning).toBe("");
  });

  it("chat:reasoning 无 tool_id → 消息级归并（旧服务端/最终答复前思考）", () => {
    let s = createChatState();
    s = applyChatEvent(s, ev("chat:tool", { tool_id: "call_1", name: "terminal", input_summary: "ls" }));
    s = applyChatEvent(s, ev("chat:reasoning", { text: "整理结论" }));
    const msg = s.messages[0];
    expect(msg.reasoning).toBe("整理结论");
    expect(msg.tools[0].reasoning).toBe("");
  });

  it("chat:reasoning 带 tool_id 但工具尚未到达 → 退回消息级", () => {
    let s = createChatState();
    s = applyChatEvent(s, ev("chat:reasoning", { tool_id: "call_9", text: "提前到达的推理" }));
    const msg = s.messages[0];
    expect(msg.reasoning).toBe("提前到达的推理");
    expect(msg.tools).toHaveLength(0);
  });

  it("并行工具：推理按各自 tool_id 各归各，不串", () => {
    let s = createChatState();
    s = applyChatEvent(s, ev("chat:tool", { tool_id: "call_1", name: "terminal", input_summary: "a" }));
    s = applyChatEvent(s, ev("chat:tool", { tool_id: "call_2", name: "terminal", input_summary: "b" }));
    s = applyChatEvent(s, ev("chat:reasoning", { tool_id: "call_2", text: "第二个" }));
    s = applyChatEvent(s, ev("chat:reasoning", { tool_id: "call_1", text: "第一个" }));
    const msg = s.messages[0];
    expect(msg.tools[0].reasoning).toBe("第一个");
    expect(msg.tools[1].reasoning).toBe("第二个");
  });

  it("history 结构化 reasoning.steps 按 tool_id 挂回工具行，消息级为空", () => {
    const history: ChatHistoryMessage[] = [
      {
        id: 1, role: "assistant", content: "结论",
        reasoning: { steps: [{ tool_id: "call_1", text: "先看拓扑" }] },
        tools: [{ name: "terminal", input_summary: "a", output_summary: "out", ok: true, tool_id: "call_1" }],
      },
    ];
    const s = stateFromHistory(history, false);
    expect(s.messages[0].tools[0].reasoning).toBe("先看拓扑");
    expect(s.messages[0].reasoning).toBe("");
    expect(s.messages[0].content).toBe("结论");
  });

  it("history 旧单值 reasoning 仍读消息级（向后兼容）", () => {
    const history: ChatHistoryMessage[] = [
      { id: 1, role: "assistant", content: "ok", reasoning: "hidden chain", tools: [] },
    ];
    const s = stateFromHistory(history, false);
    expect(s.messages[0].reasoning).toBe("hidden chain");
  });
});

describe("批四十二 §BH 跨会话审批裁决回写", () => {
  const withCard = (sid: string, approvalId: string): [string, ChatTurnState] => {
    let s = createChatState();
    s = applyChatEvent(s, ev("chat:approval_pending", { approval_id: approvalId, command: "rm -rf x", env: "test" }));
    return [sid, s];
  };

  it("只重建含该审批卡的会话槽，其它槽原引用不动", () => {
    const [, stateA] = withCard("A", "apv_1");
    const [, stateB] = withCard("B", "apv_2");
    const states = { A: stateA, B: stateB };
    const next = markApprovalResolvedInSessions(states, "apv_1", "approved");
    expect(next.A).not.toBe(stateA);
    expect(next.A.messages[0].approvals[0].status).toBe("approved");
    expect(next.A.messages[0].steps[0].status).toBe("approved");
    expect(next.B).toBe(stateB); // 无匹配卡 → 原引用
  });

  it("无匹配卡时整体返回原引用（幂等，不触发重渲染）", () => {
    const [, stateA] = withCard("A", "apv_1");
    const states = { A: stateA };
    const next = markApprovalResolvedInSessions(states, "nope", "approved");
    expect(next).toBe(states);
  });
});
