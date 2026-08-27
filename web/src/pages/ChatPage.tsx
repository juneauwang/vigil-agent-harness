import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type FormEvent, type SetStateAction } from "react";
import {
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  Loader2,
  MessageSquarePlus,
  Send,
  ShieldAlert,
  Square,
  TerminalSquare,
  User,
  Wrench,
  XCircle,
  X,
  Brain,
  HelpCircle,
  ListOrdered,
  Coins,
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
import type { ChatContextUsage, ChatModelOption, ChatSessionSummary, ChatUsageResponse, UsageAnalyticsResponse } from "@/lib/api";
import {
  approvalIsTimedOut,
  applyChatEvent,
  chatInputDisabled,
  clarifyIsTimedOut,
  createChatState,
  markClarifyResolved,
  markApprovalResolved,
  markApprovalResolvedInSessions,
  markTurnInterrupted,
  pushUserMessage,
  stateFromHistory,
  toggleToolExpanded,
  type ChatApprovalCard,
  type ChatClarifyCard,
  type ChatMessage,
  type ChatStepStatus,
  type ChatTurnState,
} from "@/lib/chat";
import { subscribeApprovalResolved } from "@/lib/approvalEvents";
import { Markdown } from "@/components/Markdown";
import StopButton from "@/components/StopButton";
import { cn } from "@/lib/ops";

/**
 * 对话 Session 页（批三十一，UI 壳核心价值页）：
 * POST /api/chat/sessions + GET /api/chat/sessions + POST
 * /api/chat/sessions/{id}/messages（SSE：chat:delta/chat:reasoning/tool/
 * tool_result/approval_pending/done/error）。流式打字机渲染；工具调用折叠行；
 * 审批卡弹在消息流内（走现有 POST /api/approvals/{id}/approve|deny）；同会话
 * 串行，agent 忙时输入禁用。
 *
 * 批四十一：工具输出按服务端 tool_id 挂接（§5）；推理折叠展示（§3）；工具/
 * 审批有序步骤序列（§4）；停止后 busy 校验（§23）；审批详情完整展示（§6）；
 * 切回 busy 指示恢复（§7）；会话级模型下拉（§8）。
 */

const STEP_STATUS_LABEL: Record<ChatStepStatus, string> = {
  running: "进行中",
  done: "完成",
  failed: "失败",
  pending: "等待审批",
  approved: "已批准",
  denied: "已拒绝",
  answered: "已回答",
  timed_out: "已超时",
};

const STEP_STATUS_CLASS: Record<ChatStepStatus, string> = {
  running: "bg-sky-500/15 text-sky-600 dark:text-sky-400",
  done: "bg-[var(--vigil-ok)]/15 text-[var(--vigil-ok)]",
  failed: "bg-red-500/15 text-red-600 dark:text-red-400",
  pending: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
  approved: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  denied: "bg-red-500/15 text-red-600 dark:text-red-400",
  answered: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  timed_out: "bg-red-500/15 text-red-600 dark:text-red-400",
};

/** 推理过程折叠块（批四十一 §3）：默认折叠为一行摘要，展开看完整文本。 */
function ReasoningBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const trimmed = text.trim();
  if (!trimmed) return null;
  const summary = trimmed.split("\n")[0].slice(0, 80) || "推理过程";
  return (
    <div className="mt-2 overflow-hidden rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-xs hover:bg-black/5 dark:hover:bg-white/5"
      >
        {open ? (
          <ChevronDown className="size-3.5 shrink-0 text-[var(--vigil-muted)]" />
        ) : (
          <ChevronRight className="size-3.5 shrink-0 text-[var(--vigil-muted)]" />
        )}
        <Brain className="size-3.5 shrink-0 text-violet-500" />
        <span className="font-medium">查看推理过程（{trimmed.length} 字）</span>
        <span className="ml-auto min-w-0 flex-1 truncate text-[var(--vigil-muted)]">
          {open ? "" : summary}
        </span>
      </button>
      {open && (
        <pre className="scroll-thin max-h-72 overflow-y-auto whitespace-pre-wrap break-words border-t border-[var(--vigil-border)] px-2.5 py-2 font-mono text-[11px] leading-relaxed text-[var(--vigil-text)] opacity-90">
          {trimmed}
        </pre>
      )}
    </div>
  );
}

function ToolRow({
  tool,
  stepNo,
  status,
  onToggle,
}: {
  tool: ChatMessage["tools"][number];
  stepNo: number;
  status: ChatStepStatus;
  onToggle: () => void;
}) {
  const [reasoningOpen, setReasoningOpen] = useState(false);
  const reasoning = (tool.reasoning ?? "").trim();
  const reasoningSummary = reasoning.split("\n")[0].slice(0, 80) || "该步推理";
  return (
    <div className="overflow-hidden rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)]">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={tool.expanded}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-xs hover:bg-black/5 dark:hover:bg-white/5"
      >
        <span className="w-4 shrink-0 text-center font-mono text-[10px] text-[var(--vigil-muted)]">
          {stepNo}
        </span>
        {tool.expanded ? (
          <ChevronDown className="size-3.5 shrink-0 text-[var(--vigil-muted)]" />
        ) : (
          <ChevronRight className="size-3.5 shrink-0 text-[var(--vigil-muted)]" />
        )}
        <Wrench className="size-3.5 shrink-0 text-sky-500" />
        <span className="font-mono font-medium">{tool.name}</span>
        <span
          className={cn(
            "ml-auto shrink-0 rounded px-1.5 py-px text-[10px]",
            STEP_STATUS_CLASS[status],
          )}
        >
          {STEP_STATUS_LABEL[status]}
        </span>
        {tool.ok !== undefined && tool.ok === false && (
          <span className="shrink-0 text-[10px] text-[var(--vigil-error)]">✗</span>
        )}
      </button>
      {reasoning && (
        <div className="border-t border-[var(--vigil-border)]">
          <button
            type="button"
            onClick={() => setReasoningOpen((v) => !v)}
            aria-expanded={reasoningOpen}
            data-testid={`tool-reasoning-${tool.id}`}
            className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[11px] hover:bg-black/5 dark:hover:bg-white/5"
          >
            {reasoningOpen ? (
              <ChevronDown className="size-3 shrink-0 text-[var(--vigil-muted)]" />
            ) : (
              <ChevronRight className="size-3 shrink-0 text-[var(--vigil-muted)]" />
            )}
            <Brain className="size-3 shrink-0 text-violet-500" />
            <span className="font-medium">该步推理（{reasoning.length} 字）</span>
            <span className="ml-auto min-w-0 flex-1 truncate text-[var(--vigil-muted)]">
              {reasoningOpen ? "" : reasoningSummary}
            </span>
          </button>
          {reasoningOpen && (
            <pre className="scroll-thin max-h-72 overflow-y-auto whitespace-pre-wrap break-words border-t border-[var(--vigil-border)] px-2.5 py-2 font-mono text-[11px] leading-relaxed text-[var(--vigil-text)] opacity-90">
              {reasoning}
            </pre>
          )}
        </div>
      )}
      {tool.expanded && (
        <div className="space-y-1.5 border-t border-[var(--vigil-border)] px-2.5 py-2">
          {tool.inputSummary && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-[var(--vigil-muted)]">输入</div>
              <pre className="scroll-thin whitespace-pre-wrap break-words font-mono text-[11px] text-[var(--vigil-text)] opacity-85">
                {tool.inputSummary}
              </pre>
            </div>
          )}
          {tool.outputSummary !== undefined && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-[var(--vigil-muted)]">输出</div>
              <pre className="scroll-thin max-h-64 overflow-y-auto whitespace-pre-wrap break-words font-mono text-[11px] text-[var(--vigil-text)] opacity-85">
                {tool.outputSummary || "（空输出）"}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** 审批卡：长命令可滚动 + 叙述完整展示（批四十一 §6），批准/拒绝后状态标签。
 * 批四十二 §BK：展示触发该审批的推理摘要（一行可展开，默认折叠）——盲批风险
 * 防护，用户可先看 agent 为什么执行这条命令再决定。 */
function ApprovalCard({
  card,
  onResolve,
  triggerReasoning,
}: {
  card: ChatApprovalCard;
  onResolve: (card: ChatApprovalCard, status: "approved" | "denied") => void;
  triggerReasoning?: string;
}) {
  const busy = card.status === "approved" || card.status === "denied";
  const [expanded, setExpanded] = useState(false);
  const [reasonOpen, setReasonOpen] = useState(false);
  const triggerText = (triggerReasoning ?? "").trim();
  const triggerSummary = triggerText.split("\n")[0].slice(0, 80) || "触发推理";
  const longCommand = (card.command ?? "").length > 80;
  const [timedOut, setTimedOut] = useState(false);
  useEffect(() => {
    const check = () => setTimedOut(approvalIsTimedOut(card));
    check();
    const timer = window.setInterval(check, 1000);
    return () => window.clearInterval(timer);
  }, [card.status, card.timeoutAt]);
  return (
    <div
      className={cn(
        "flex flex-col gap-1.5 rounded-md border px-3 py-2 text-xs",
        card.status === "approved"
          ? "border-emerald-500/50 bg-emerald-500/10"
          : card.status === "denied"
            ? "border-red-500/50 bg-red-500/10"
            : "border-amber-500/50 bg-amber-500/10",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <ShieldAlert className={cn("size-4 shrink-0", card.status === "approved" ? "text-emerald-500" : card.status === "denied" ? "text-red-500" : "text-amber-500")} />
        <span className="font-medium">该命令需要审批</span>
        {card.grade && <span className="rounded bg-amber-500/15 px-1.5 py-px text-[10px] text-amber-600 dark:text-amber-400">L{card.grade}</span>}
        {card.env && <span className="text-[var(--vigil-muted)]">{card.env}</span>}
        <span className="ml-auto shrink-0 text-[10px]">
          {card.status === "pending" && timedOut ? (
            <span className="text-red-500">审批超时，已终止</span>
          ) : card.status === "approved" ? (
            <span className="text-emerald-500">已批准</span>
          ) : card.status === "denied" ? (
            <span className="text-red-500">已拒绝</span>
          ) : (
            <span className="text-amber-500">等待审批</span>
          )}
        </span>
      </div>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="min-w-0 text-left font-mono text-[11px]"
        title={longCommand ? "点击展开/收起完整命令" : card.command}
      >
        <span className="block whitespace-pre-wrap break-words">
          {expanded || !longCommand ? card.command : `${card.command.slice(0, 80)}…`}
        </span>
      </button>
      {expanded && longCommand && (
        <button
          type="button"
          onClick={() => setExpanded(false)}
          className="self-start text-[10px] text-[var(--vigil-muted)] underline"
        >
          收起
        </button>
      )}
      {card.description && (
        <span className="whitespace-pre-wrap break-words text-[11px] text-[var(--vigil-muted)]">
          {card.description}
        </span>
      )}
      {triggerText && (
        <div className="overflow-hidden rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)]">
          <button
            type="button"
            onClick={() => setReasonOpen((v) => !v)}
            aria-expanded={reasonOpen}
            data-testid={`approval-reasoning-${card.approvalId}`}
            className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[11px] hover:bg-black/5 dark:hover:bg-white/5"
          >
            {reasonOpen ? (
              <ChevronDown className="size-3 shrink-0 text-[var(--vigil-muted)]" />
            ) : (
              <ChevronRight className="size-3 shrink-0 text-[var(--vigil-muted)]" />
            )}
            <Brain className="size-3 shrink-0 text-violet-500" />
            <span className="font-medium">触发推理（{triggerText.length} 字）</span>
            <span className="ml-auto min-w-0 flex-1 truncate text-[var(--vigil-muted)]">
              {reasonOpen ? "" : triggerSummary}
            </span>
          </button>
          {reasonOpen && (
            <pre className="scroll-thin max-h-64 overflow-y-auto whitespace-pre-wrap break-words border-t border-[var(--vigil-border)] px-2.5 py-2 font-mono text-[11px] leading-relaxed text-[var(--vigil-text)] opacity-90">
              {triggerText}
            </pre>
          )}
        </div>
      )}
      {card.status === "pending" && !timedOut && (
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => onResolve(card, "approved")}
            className="vigil-btn h-7 whitespace-nowrap border border-emerald-500/50 px-2 text-xs text-emerald-600 dark:text-emerald-400"
          >
            <Check className="size-3.5" /> 批准
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onResolve(card, "denied")}
            className="vigil-btn h-7 whitespace-nowrap border border-red-500/50 px-2 text-xs text-red-600 dark:text-red-400"
          >
            <X className="size-3.5" /> 拒绝
          </button>
        </div>
      )}
    </div>
  );
}

/** 批四十九：对话流内 clarify 卡（问题 + choices 按钮 + 自由文本 + 提交/取消
 * + 超时倒计时）。非全局弹窗（clarify 是"问题"，审批是"命令确认"）。提交 →
 * POST /api/chat/sessions/{id}/clarify → 卡片收起、agent 回合继续；超时 →
 * "已超时，agent 自行决定"（后端 timeout 语义，前端倒计时同步显示）。 */
function ClarifyCard({
  card,
  onResolve,
}: {
  card: ChatClarifyCard;
  onResolve: (card: ChatClarifyCard, answer: string | string[]) => void;
}) {
  // error 状态可重试（提交失败 → 按钮仍可用）；answered/timed_out 收口。
  const busy = card.status === "answered" || card.status === "timed_out";
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [custom, setCustom] = useState("");
  const [timedOut, setTimedOut] = useState(false);
  const [remaining, setRemaining] = useState<number | null>(null);
  useEffect(() => {
    const check = () => {
      setTimedOut(clarifyIsTimedOut(card));
      if (card.timeoutAt) {
        const ms = Date.parse(card.timeoutAt) - Date.now();
        setRemaining(Number.isNaN(ms) ? null : Math.max(0, Math.floor(ms / 1000)));
      } else {
        setRemaining(null);
      }
    };
    check();
    const timer = window.setInterval(check, 1000);
    return () => window.clearInterval(timer);
  }, [card.status, card.timeoutAt]);

  const expired = timedOut || card.status === "timed_out";
  const hasChoices = (card.choices?.length ?? 0) > 0;
  const toggle = (choice: string) => {
    setSelected((prev) => {
      const nextSet = new Set(prev);
      if (card.multiSelect) {
        if (nextSet.has(choice)) nextSet.delete(choice);
        else nextSet.add(choice);
      } else {
        nextSet.clear();
        nextSet.add(choice);
      }
      return nextSet;
    });
  };
  const submit = () => {
    if (busy || expired) return;
    if (card.multiSelect && hasChoices) {
      const picked = Array.from(selected);
      const customText = custom.trim();
      onResolve(card, picked.length > 0 ? picked : customText ? [customText] : []);
      return;
    }
    const customText = custom.trim();
    if (hasChoices && !customText) {
      const picked = Array.from(selected);
      onResolve(card, picked.length > 0 ? picked[0] : "");
      return;
    }
    onResolve(card, customText);
  };
  const cancel = () => {
    if (busy || expired) return;
    onResolve(card, "");
  };
  const fmt = (s: number): string => {
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
  };

  return (
    <div
      data-testid={`clarify-card-${card.clarifyId}`}
      className={cn(
        "flex flex-col gap-2 rounded-md border px-3 py-2 text-xs",
        card.status === "answered"
          ? "border-emerald-500/50 bg-emerald-500/10"
          : expired
            ? "border-red-500/50 bg-red-500/10"
            : "border-sky-500/50 bg-sky-500/10",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <HelpCircle className="size-4 shrink-0 text-sky-500" />
        <span className="font-medium">需要你回答</span>
        {card.status === "answered" ? (
          <span className="ml-auto text-emerald-500">已提交，agent 继续</span>
        ) : expired ? (
          <span className="ml-auto text-red-500">已超时，agent 自行决定</span>
        ) : card.status === "error" ? (
          <span className="ml-auto text-red-500">提交失败，请重试</span>
        ) : (
          <span className="ml-auto shrink-0 text-[10px]">
            {remaining !== null && (
              <span className={cn(remaining <= 60 ? "text-red-500" : "text-[var(--vigil-muted)]")}>
                剩余 {fmt(remaining)}
              </span>
            )}
          </span>
        )}
      </div>
      <div className="whitespace-pre-wrap break-words text-[13px] text-[var(--vigil-text)]">
        {card.question}
      </div>
      {hasChoices && (
        <div className="flex flex-wrap gap-1.5">
          {(card.choices ?? []).map((choice) => {
            const on = selected.has(choice);
            return (
              <button
                key={choice}
                type="button"
                disabled={busy || expired}
                onClick={() => toggle(choice)}
                aria-pressed={on}
                className={cn(
                  "rounded border px-2 py-1 text-[11px] transition-colors disabled:opacity-50",
                  on
                    ? "border-sky-500/70 bg-sky-500/15 text-sky-600 dark:text-sky-400"
                    : "border-[var(--vigil-border)] bg-[var(--vigil-card)] text-[var(--vigil-text)] hover:bg-[var(--vigil-muted-bg)]",
                )}
              >
                {card.multiSelect && <span className="mr-1">{on ? "☑" : "☐"}</span>}
                {choice}
              </button>
            );
          })}
        </div>
      )}
      <input
        value={custom}
        onChange={(e) => setCustom(e.target.value)}
        disabled={busy || expired}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
        placeholder={hasChoices ? "其他（自行输入）…" : "输入回答…"}
        data-testid={`clarify-input-${card.clarifyId}`}
        className="h-8 min-w-0 rounded border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-2 text-xs text-[var(--vigil-text)] outline-none placeholder:text-[var(--vigil-muted)]/60 disabled:opacity-50"
      />
      {card.status === "pending" && !expired && (
        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={cancel}
            className="vigil-btn h-7 border border-[var(--vigil-border)] px-2 text-xs text-[var(--vigil-muted)] disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={submit}
            data-testid={`clarify-submit-${card.clarifyId}`}
            className="vigil-btn h-7 whitespace-nowrap border border-sky-500/50 px-2 text-xs text-sky-600 dark:text-sky-400 disabled:opacity-50"
          >
            提交
          </button>
        </div>
      )}
    </div>
  );
}

function StepList({
  msg,
  onToggleTool,
  onResolveApproval,
  onResolveClarify,
}: {
  msg: ChatMessage;
  onToggleTool: (toolId: number) => void;
  onResolveApproval: (card: ChatApprovalCard, status: "approved" | "denied") => void;
  onResolveClarify: (card: ChatClarifyCard, answer: string | string[]) => void;
}) {
  const toolById = new Map(msg.tools.map((t) => [t.id, t]));
  const approvalById = new Map(msg.approvals.map((a) => [a.approvalId, a]));
  const clarifyById = new Map(msg.clarifies.map((c) => [c.clarifyId, c]));
  const steps = msg.steps;
  if (steps.length === 0) return null;

  // 批四十二 §BK：审批的"触发推理"= 其前最近一个工具步骤的该步推理（SSE 中
  // 推理先随 chat:tool 到、审批在工具执行内到，顺序天然满足）。
  const triggerReasoningFor = (index: number): string => {
    for (let i = index - 1; i >= 0; i--) {
      const prev = steps[i];
      if (prev.kind === "tool") {
        const tool = toolById.get(Number(prev.ref));
        return tool?.reasoning ?? "";
      }
    }
    return "";
  };

  const current = steps.find((s) => s.status === "running" || s.status === "pending");
  return (
    <div data-testid="step-list" className="mt-2 space-y-1.5">
      {steps.length > 1 && (
        <div className="flex items-center gap-1.5 text-[10px] text-[var(--vigil-muted)]">
          <ListOrdered className="size-3" />
          <span>
            共 {steps.length} 步
            {current && <> · 当前：{STEP_STATUS_LABEL[current.status]}</>}
          </span>
        </div>
      )}
      <ol className="space-y-1.5">
        {steps.map((step, i) => {
          const no = i + 1;
          if (step.kind === "tool") {
            const tool = toolById.get(Number(step.ref));
            if (!tool) return null;
            return (
              <li key={`tool-${step.ref}`}>
                <ToolRow tool={tool} stepNo={no} status={step.status} onToggle={() => onToggleTool(tool.id)} />
              </li>
            );
          }
          if (step.kind === "approval") {
            const card = approvalById.get(String(step.ref));
            if (!card) return null;
            return (
              <li key={`approval-${step.ref}`} className="ml-3">
                <div className="flex items-center gap-1.5 text-[10px] text-[var(--vigil-muted)]">
                  <span className="w-4 text-center font-mono">{no}</span>
                  <span>审批</span>
                </div>
                <ApprovalCard
                  card={card}
                  onResolve={onResolveApproval}
                  triggerReasoning={triggerReasoningFor(i)}
                />
              </li>
            );
          }
          const card = clarifyById.get(String(step.ref));
          if (!card) return null;
          return (
            <li key={`clarify-${step.ref}`} className="ml-3">
              <div className="flex items-center gap-1.5 text-[10px] text-[var(--vigil-muted)]">
                <span className="w-4 text-center font-mono">{no}</span>
                <span>clarify</span>
              </div>
              <ClarifyCard card={card} onResolve={onResolveClarify} />
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function MessageBubble({ msg, onToggleTool, onResolveApproval, onResolveClarify }: {
  msg: ChatMessage;
  onToggleTool: (toolId: number) => void;
  onResolveApproval: (card: ChatApprovalCard, status: "approved" | "denied") => void;
  onResolveClarify: (card: ChatClarifyCard, answer: string | string[]) => void;
}) {
  if (msg.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-lg rounded-br-sm border border-[var(--vigil-border)] bg-[var(--vigil-primary)]/10 px-3 py-2">
          <div className="mb-1 flex items-center gap-1.5 text-[10px] text-[var(--vigil-muted)]">
            <User className="size-3" /> 你
          </div>
          <div className="whitespace-pre-wrap break-words text-sm">{msg.content}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex justify-start">
      <div className="max-w-[92%] min-w-0 rounded-lg rounded-bl-sm border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-3 py-2 shadow-[var(--vigil-shadow)]">
        <div className="mb-1 flex items-center gap-1.5 text-[10px] text-[var(--vigil-muted)]">
          <Bot className="size-3" /> Vigil
          {msg.streaming && <Loader2 className="size-3 animate-spin text-[var(--vigil-muted)]" />}
        </div>
        {msg.interrupted && (
          <div className="mb-2 flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-600 dark:text-amber-400">
            <Square className="size-3.5 shrink-0" /> 已停止
          </div>
        )}
        {msg.error && (
          <div className="flex items-center gap-2 rounded-md border border-red-500/50 bg-red-500/10 px-3 py-2 text-xs text-red-600 dark:text-red-400">
            <XCircle className="size-4 shrink-0" />
            {msg.error}
          </div>
        )}
        <StepList
          msg={msg}
          onToggleTool={onToggleTool}
          onResolveApproval={onResolveApproval}
          onResolveClarify={onResolveClarify}
        />
        <ReasoningBlock text={msg.reasoning} />
        {msg.content ? (
          <div data-testid="assistant-content">
            <Markdown text={msg.content} />
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** 批四十一 §23：停止后校验注册表 busy 翻转（最多 10s），翻转后强制重拉
 * 历史 + 保留"已停止"本地行；未翻转给可见警告，不静默残留。 */
async function verifyBusyCleared(
  sid: string,
  setStates: Dispatch<SetStateAction<Record<string, ChatTurnState>>>,
  setStopWarning: Dispatch<SetStateAction<string | null>>,
  setBusyMap?: Dispatch<SetStateAction<Record<string, boolean>>>,
): Promise<void> {
  const deadline = Date.now() + 10_000;
  try {
    while (Date.now() < deadline) {
      const resp = await api.listChatSessions();
      const s = (resp.sessions ?? []).find((x) => x.id === sid);
      if (s && !s.busy) {
        const h = await api.getChatHistory(sid);
        setStates((prev) => {
          const recovered = stateFromHistory(h.messages ?? [], false);
          return { ...prev, [sid]: markTurnInterrupted(recovered) };
        });
        setStopWarning(null);
        return;
      }
      await new Promise((r) => setTimeout(r, 2000));
    }
    setStopWarning("服务端仍显示忙碌（可能仍在收尾）。可在会话列表刷新或稍后重试。");
    // 解除本地/注册表快照 busy，避免输入永久禁用——若后端真还忙，发送会被
    // 409 busy 拦下并显示错误，用户仍可操作而非静默卡死。
    setBusyMap?.((prev) => ({ ...prev, [sid]: false }));
  } catch {
    setStopWarning("未能确认忙碌状态已清除，可在会话列表刷新重试。");
    setBusyMap?.((prev) => ({ ...prev, [sid]: false }));
  }
}

// ── 批六十四：token 用量面板（当前会话实时 + 历史累计 + 费用）─────────────

const USAGE_SOURCE_LABEL: Record<string, string> = {
  manual: "手动价",
  online: "在线拉取",
  builtin: "内置估算",
};

function formatTokens(n: number | null | undefined): string {
  const v = Number(n ?? 0);
  return Number.isFinite(v) ? v.toLocaleString() : "0";
}

/** 批八十一：context 用量紧凑格式（37K / 131K；拿不到 → "—"）。 */
function formatCompactTokens(n: number | null | undefined): string {
  const v = Number(n ?? 0);
  if (!Number.isFinite(v) || v < 0) return "—";
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `${Math.round(v / 1_000)}K`;
  return String(v);
}

function contextUsageLabel(u: ChatContextUsage | null | undefined): string {
  if (!u || u.used_tokens === null || u.used_tokens === undefined) return "—";
  if (u.limit_tokens) {
    return `${formatCompactTokens(u.used_tokens)} / ${formatCompactTokens(u.limit_tokens)}`;
  }
  return formatCompactTokens(u.used_tokens);
}

function formatCost(cost: number | null | undefined, currency: string | null | undefined): string | null {
  const v = Number(cost);
  if (cost === null || cost === undefined || !Number.isFinite(v)) return null;
  const symbol = currency === "cny" ? "¥" : "$";
  return `约 ${symbol}${v.toFixed(2)}`;
}

function localTodayKey(): string {
  const d = new Date();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

function UsagePanel({
  activeId,
  usage,
  usageLoading,
  usageError,
  analytics,
  analyticsLoading,
}: {
  activeId: string | null;
  usage: ChatUsageResponse | null;
  usageLoading: boolean;
  usageError: string | null;
  analytics: UsageAnalyticsResponse | null;
  analyticsLoading: boolean;
}) {
  const daily = analytics?.daily ?? [];
  const todayRow = daily.find((r) => r.day === localTodayKey()) ?? daily[daily.length - 1];
  const totals = analytics?.totals;
  const price = usage?.price ?? null;
  const costText = price ? formatCost(usage?.cost ?? null, usage?.cost_currency ?? null) : null;
  const sourceLabel = price ? USAGE_SOURCE_LABEL[price.source] ?? price.source : null;

  const row = (label: string, value: string, cost?: string | null) => (
    <div className="flex items-center justify-between gap-2">
      <span className="text-[var(--vigil-muted)]">{label}</span>
      <span className="font-mono text-[var(--vigil-text)]">{value}{cost ? `（${cost}）` : null}</span>
    </div>
  );

  return (
    <div data-testid="usage-panel" className="mb-3 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)] p-3 text-xs">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Coins className="size-3.5 text-[var(--vigil-muted)]" />
        <span className="font-semibold">用量</span>
        <span className="text-[var(--vigil-muted)]">
          当前会话 token 实时读会话记录；今天/近 30 天含全部会话（CLI / 网关 / chat）。
        </span>
      </div>
      <div className="grid gap-3 md:grid-cols-3">
        <div className="space-y-1 rounded border border-[var(--vigil-border)] p-2">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium">当前会话</span>
            {usageLoading && <Loader2 className="size-3 animate-spin text-[var(--vigil-muted)]" />}
          </div>
          {!activeId ? (
            <div className="text-[var(--vigil-muted)]">创建会话后显示用量</div>
          ) : usageError ? (
            <div className="text-red-600 dark:text-red-400">{usageError}</div>
          ) : usage ? (
            <>
              {row("模型", usage.model || "—")}
              {row("输入", formatTokens(usage.input_tokens))}
              {row("输出", formatTokens(usage.output_tokens))}
              {row("总计", formatTokens(usage.total_tokens))}
              {costText ? (
                <div className="flex items-center justify-between gap-2 pt-1">
                  <span className="text-[var(--vigil-muted)]">费用</span>
                  <span className="font-mono text-[var(--vigil-ok)]">
                    {costText}
                    {sourceLabel ? <span className="ml-1 text-[10px] text-[var(--vigil-muted)]">（{sourceLabel}）</span> : null}
                  </span>
                </div>
              ) : (
                <div className="pt-1 text-[10px] text-[var(--vigil-muted)]">价格不可用，仅显示 token</div>
              )}
            </>
          ) : (
            <div className="text-[var(--vigil-muted)]">加载中…</div>
          )}
        </div>

        <div className="space-y-1 rounded border border-[var(--vigil-border)] p-2">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium">今天</span>
            {analyticsLoading && <Loader2 className="size-3 animate-spin text-[var(--vigil-muted)]" />}
          </div>
          {row("输入", formatTokens(todayRow?.input_tokens))}
          {row("输出", formatTokens(todayRow?.output_tokens))}
          {row("总计", formatTokens((todayRow?.input_tokens ?? 0) + (todayRow?.output_tokens ?? 0)))}
          {row("费用", "—", formatCost(todayRow?.estimated_cost ?? null, "usd"))}
        </div>

        <div className="space-y-1 rounded border border-[var(--vigil-border)] p-2">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium">近 30 天</span>
            {analyticsLoading && <Loader2 className="size-3 animate-spin text-[var(--vigil-muted)]" />}
          </div>
          {row("输入", formatTokens(totals?.total_input))}
          {row("输出", formatTokens(totals?.total_output))}
          {row("总计", formatTokens((totals?.total_input ?? 0) + (totals?.total_output ?? 0)))}
          {row("费用", "—", formatCost(totals?.total_estimated_cost ?? null, "usd"))}
        </div>
      </div>
    </div>
  );
}

export default function ChatPage() {
  const [sessions, setSessions] = useState<ChatSessionSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  // 批三十三：会话状态分槽——每个会话独立消息列表 + busy/SSE 状态（按
  // sessionId 存 map）。切页/切回不再丢消息；A 忙时可切到 B 发消息；切回
  // A 由历史端点 + 轮询恢复现场。
  const [states, setStates] = useState<Record<string, ChatTurnState>>({});
  // 批四十一 §7：注册表 busy 快照（每次 listChatSessions 更新）——切回时
  // 即使本地槽位丢失，也能恢复"处理中"指示。
  const [busyMap, setBusyMap] = useState<Record<string, boolean>>({});
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [stopWarning, setStopWarning] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState(false);
  const [stopping, setStopping] = useState(false);
  // 批四十一 §8：可选模型目录 + 每会话选择。
  const [modelOptions, setModelOptions] = useState<ChatModelOption[]>([]);
  const [modelSelections, setModelSelections] = useState<Record<string, string>>({});
  // 批六十四：token 用量面板（打开时拉一次；会话切换时重拉）。
  const [usageOpen, setUsageOpen] = useState(false);
  const [usageData, setUsageData] = useState<ChatUsageResponse | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageError, setUsageError] = useState<string | null>(null);
  const [analytics, setAnalytics] = useState<UsageAnalyticsResponse | null>(null);
  const [analyticsLoading, setAnalyticsLoading] = useState(false);
  const abortRefs = useRef<Record<string, AbortController>>({});
  const loadedRef = useRef<Set<string>>(new Set());
  const bottomRef = useRef<HTMLDivElement>(null);

  const activeState = (activeId && states[activeId]) || createChatState();
  // 有效 busy：本地槽位 busy 或注册表快照 busy（§7：busi指示不依赖消息流）。
  const activeBusy = activeBusyOf(activeId, states, busyMap);
  // 批八十一：当前会话 context 用量（>80% 变色 + 建议新开）。
  const activeUsage = activeId
    ? (sessions.find((s) => s.id === activeId)?.context_usage ?? null)
    : null;
  const usagePct = activeUsage?.pct ?? null;
  const usageHot = usagePct !== null && usagePct > 80;

  const trackSessions = useCallback((list: ChatSessionSummary[]) => {
    const map: Record<string, boolean> = {};
    for (const s of list) map[s.id] = Boolean(s.busy);
    setBusyMap((prev) => ({ ...prev, ...map }));
    return list;
  }, []);

  const refreshSessions = useCallback(async () => {
    try {
      const resp = await api.listChatSessions();
      if (resp.error) return [];
      const list = resp.sessions ?? [];
      trackSessions(list);
      setSessions(list);
      return list;
    } catch {
      return [];
    }
  }, [trackSessions]);

  const loadSessionHistory = useCallback(async (id: string) => {
    if (loadedRef.current.has(id)) return;
    loadedRef.current.add(id);
    try {
      const resp = await api.getChatHistory(id);
      setStates((prev) => {
        const existing = prev[id];
        if (existing && existing.messages.length > 0) return prev;
        return { ...prev, [id]: stateFromHistory(resp.messages ?? [], Boolean(resp.busy)) };
      });
      // 同步注册表 busy 快照（历史端点自带 busy）。
      setBusyMap((prev) => ({ ...prev, [id]: Boolean(resp.busy) }));
    } catch {
      loadedRef.current.delete(id);
    }
  }, []);

  const createSession = useCallback(
    async (model?: string) => {
      setError(null);
      setBusyAction(true);
      try {
        // 批五十一：模型条目带 provider（custom:<slug> 等），随创建提交路由。
        const entry = model ? modelOptions.find((m) => m.id === model) : undefined;
        const resp = await api.createChatSession(model, entry?.provider);
        const sid = resp.chat_session_id;
        setActiveId(sid);
        loadedRef.current.add(sid);
        setStates((prev) => ({ ...prev, [sid]: createChatState() }));
        setModelSelections((prev) => ({
          ...prev,
          [sid]: resp.model ?? modelOptions.find((m) => m.default)?.id ?? "",
        }));
        await refreshSessions();
      } catch (e) {
        setError(e instanceof ApiError ? `[${e.code}] ${e.message}` : e instanceof Error ? e.message : String(e));
      } finally {
        setBusyAction(false);
      }
    },
    [refreshSessions, modelOptions],
  );

  // 初始化：拉可选模型目录 + 列活会话；无则新建。
  useEffect(() => {
    let alive = true;
    void api.getModels().then((resp) => {
      if (!alive) return;
      setModelOptions(resp.models ?? []);
      const def = resp.default_model ?? "";
      if (def) setModelSelections((prev) => ({ ...prev, __default: def }));
    }).catch(() => {});
    api
      .listChatSessions()
      .then(async (resp) => {
        if (!alive) return;
        const list = resp.sessions ?? [];
        trackSessions(list);
        setSessions(list);
        if (list.length > 0) {
          setActiveId(list[0].id);
        } else {
          try {
            const created = await api.createChatSession();
            if (!alive) return;
            setActiveId(created.chat_session_id);
            setSessions([{ id: created.chat_session_id, title: "", created_at: "", busy: false }]);
          } catch (e) {
            if (alive) setError(e instanceof Error ? e.message : String(e));
          }
        }
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
      for (const c of Object.values(abortRefs.current)) c?.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 切到无缓存的会话 → 拉历史恢复现场（1a：切页/切回消息完整）。
  useEffect(() => {
    if (!activeId) return;
    void loadSessionHistory(activeId);
  }, [activeId, loadSessionHistory]);

  // 批四十一 §7：activeId 变化/挂载时立即重拉注册表 busy（切回时恢复
  // "处理中"指示，不等 2s 轮询）。
  useEffect(() => {
    if (!activeId) return;
    let alive = true;
    void refreshSessions().then((list) => {
      if (!alive) return;
      const s = (list ?? []).find((x) => x.id === activeId);
      if (s && s.busy) {
        setStates((prev) => {
          const slot = prev[activeId];
          if (slot && slot.busy) return prev;
          // 槽位缺失或本地 busy 丢失 → 用注册表 busy 恢复指示（保留已有消息，
          // 消息列表增量事件缺失也不丢"处理中"视觉）。
          const st = slot ? { ...slot, busy: true } : { ...createChatState(), busy: true };
          return { ...prev, [activeId]: st };
        });
      }
    });
    return () => {
      alive = false;
    };
  }, [activeId, refreshSessions]);

  // 后台 turn 收尾轮询：当前会话 busy（本地或注册表快照）时轮询注册表，
  // busy 翻转 → 重拉历史拿最终内容（1b 验收③：在跑的显示"处理中"，跑完恢复）。
  useEffect(() => {
    if (!activeId || !activeBusy) return;
    const timer = window.setInterval(async () => {
      try {
        const resp = await api.listChatSessions();
        trackSessions(resp.sessions ?? []);
        const s = (resp.sessions ?? []).find((x) => x.id === activeId);
        if (s && !s.busy) {
          const h = await api.getChatHistory(activeId);
          setStates((prev) => {
            const existing = prev[activeId];
            // 批四十一 §23：stop 场景本地"已停止"行不持久化——恢复历史后
            // 若原槽位有 interrupted 标记，重挂一次，保证停止状态可见。
            const hadInterrupted = existing?.messages.some((m) => m.interrupted) ?? false;
            const st = stateFromHistory(h.messages ?? [], false);
            return { ...prev, [activeId]: hadInterrupted ? markTurnInterrupted(st) : st };
          });
          setBusyMap((prev) => ({ ...prev, [activeId]: false }));
        }
      } catch {
        // transient network hiccup — next tick retries
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [activeId, activeBusy, trackSessions]);

  // 批六十四：用量面板打开 → 拉当前会话实时 token（会话切换时重拉）+ 历史
  // 累计（全局，打开拉一次即可）。关闭时清空，避免残留旧会话数据。
  useEffect(() => {
    if (!usageOpen) return;
    let alive = true;
    setUsageLoading(true);
    setUsageError(null);
    if (activeId) {
      api
        .getChatUsage(activeId)
        .then((resp) => {
          if (!alive) return;
          if (resp.error) setUsageError(`[${resp.error.code}] ${resp.error.message}`);
          else setUsageData(resp);
        })
        .catch((e: unknown) => {
          if (!alive) return;
          setUsageData(null);
          setUsageError(e instanceof ApiError ? `[${e.code}] ${e.message}` : e instanceof Error ? e.message : String(e));
        })
        .finally(() => {
          if (alive) setUsageLoading(false);
        });
    } else {
      setUsageData(null);
      setUsageLoading(false);
    }
    return () => {
      alive = false;
    };
  }, [usageOpen, activeId]);

  useEffect(() => {
    if (!usageOpen) return;
    let alive = true;
    setAnalyticsLoading(true);
    api
      .getUsageAnalytics(30)
      .then((resp) => {
        if (alive && !resp.error) setAnalytics(resp);
      })
      .catch(() => {})
      .finally(() => {
        if (alive) setAnalyticsLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [usageOpen]);

  // 新内容自动滚底。
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [activeState.messages]);

  // 批四十二 §BH：全局审批弹窗/审批中心裁决广播 → 所有会话槽位内对应
  // approvalId 的审批卡立即回写（已批准/已拒绝），不依赖轮询或重拉历史。
  useEffect(
    () =>
      subscribeApprovalResolved((id, status) => {
        setStates((prev) => markApprovalResolvedInSessions(prev, id, status));
      }),
    [],
  );

  const send = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      const text = draft.trim();
      const sid = activeId;
      const st = (sid && states[sid]) || createChatState();
      if (!text || !sid || chatInputDisabled(st) || activeBusy) return;
      setDraft("");
      setError(null);
      setStopWarning(null);
      setStates((prev) => ({ ...prev, [sid]: pushUserMessage(prev[sid] ?? createChatState(), text) }));
      const ctrl = new AbortController();
      abortRefs.current[sid] = ctrl;
      try {
        await api.chatStream(sid, text, (ev) => {
          setStates((prev) => ({
            ...prev,
            [sid]: applyChatEvent(prev[sid] ?? createChatState(), {
              type: ev.type,
              data: (ev.data ?? {}) as Record<string, unknown>,
            }),
          }));
        }, ctrl.signal);
        void refreshSessions();
      } catch (err) {
        if (err instanceof Error && err.name === "AbortError") return;
        const msg = err instanceof ApiError ? `[${err.code}] ${err.message}` : err instanceof Error ? err.message : String(err);
        setStates((prev) => ({
          ...prev,
          [sid]: applyChatEvent(prev[sid] ?? createChatState(), { type: "chat:error", data: { message: msg } }),
        }));
      } finally {
        if (abortRefs.current[sid] === ctrl) delete abortRefs.current[sid];
      }
    },
    [activeId, draft, refreshSessions, states, activeBusy],
  );

  const stopTurn = useCallback(async () => {
    const sid = activeId;
    const st = (sid && states[sid]) || createChatState();
    if (!sid || !chatInputDisabled(st) || stopping) return;
    setStopping(true);
    setError(null);
    setStopWarning(null);
    try {
      await api.interruptChatSession(sid);
    } catch (err) {
      // 中断请求失败（网络/409 等）提示但不卡死：仍 abort SSE + 本地标记停止，
      // 后台 turn 由注册表轮询兜底复位。
      const msg = err instanceof ApiError ? `[${err.code}] ${err.message}` : err instanceof Error ? err.message : String(err);
      setError(`停止请求未送达（${msg}）；若 agent 仍在运行请稍后重试`);
    } finally {
      setStopping(false);
    }
    abortRefs.current[sid]?.abort();
    setStates((prev) => ({
      ...prev,
      [sid]: markTurnInterrupted(prev[sid] ?? createChatState()),
    }));
    void refreshSessions();
    // 批四十一 §23：本地 busy 已解除，独立校验循环核对注册表翻转。
    void verifyBusyCleared(sid, setStates, setStopWarning, setBusyMap);
  }, [activeId, refreshSessions, states, stopping]);

  const resolveApproval = useCallback(
    async (card: ChatApprovalCard, status: "approved" | "denied") => {
      const sid = activeId;
      if (!sid) return;
      setStates((prev) => ({
        ...prev,
        [sid]: markApprovalResolved(prev[sid] ?? createChatState(), card.approvalId, status === "approved" ? "approved" : "denied"),
      }));
      try {
        if (status === "approved") await api.approveApproval(card.approvalId, "once");
        else await api.denyApproval(card.approvalId);
      } catch (err) {
        const msg = err instanceof ApiError ? `[${err.code}] ${err.message}` : err instanceof Error ? err.message : String(err);
        setError(msg);
        setStates((prev) => ({
          ...prev,
          [sid]: markApprovalResolved(prev[sid] ?? createChatState(), card.approvalId, "error"),
        }));
      }
    },
    [activeId],
  );

  // 批四十九：clarify 提交 → POST /api/chat/sessions/{id}/clarify；本地先收口
  // 卡片（提交中禁用防连点），失败回滚为 error 可重试。
  const resolveClarify = useCallback(
    async (card: ChatClarifyCard, answer: string | string[]) => {
      const sid = activeId;
      if (!sid) return;
      setStates((prev) => ({
        ...prev,
        [sid]: markClarifyResolved(prev[sid] ?? createChatState(), card.clarifyId, "answered"),
      }));
      try {
        await api.answerChatClarify(sid, answer);
      } catch (err) {
        const apiErr = err instanceof ApiError ? err : null;
        // 已超时 → 卡片显示"已超时"；无挂起（回合已收尾）→ 视为已提交。
        const status: "timed_out" | "answered" | "error" =
          apiErr?.code === "clarify_timed_out"
            ? "timed_out"
            : apiErr?.code === "no_pending_clarify"
              ? "answered"
              : "error";
        if (status === "error") {
          const msg = err instanceof ApiError ? `[${err.code}] ${err.message}` : err instanceof Error ? err.message : String(err);
          setError(msg);
        }
        setStates((prev) => ({
          ...prev,
          [sid]: markClarifyResolved(prev[sid] ?? createChatState(), card.clarifyId, status),
        }));
      }
    },
    [activeId],
  );

  const switchSession = useCallback(
    (id: string) => {
      if (id === activeId) return;
      // 切走：abort 旧会话 SSE（后台 turn 继续跑，切回由轮询恢复现场）。
      if (activeId) abortRefs.current[activeId]?.abort();
      setActiveId(id);
      setError(null);
      setStopWarning(null);
    },
    [activeId],
  );

  const toggleTool = useCallback(
    (toolId: number) => {
      if (!activeId) return;
      setStates((prev) => ({
        ...prev,
        [activeId]: toggleToolExpanded(prev[activeId] ?? createChatState(), toolId),
      }));
    },
    [activeId],
  );

  // 批四十一 §8：切换会话模型（会话级，新消息生效）。批五十一：随 model 提交 provider 路由。
  const changeSessionModel = useCallback(
    async (sid: string, model: string) => {
      if (!sid || !model || modelSelections[sid] === model) return;
      setModelSelections((prev) => ({ ...prev, [sid]: model }));
      try {
        const entry = modelOptions.find((m) => m.id === model);
        const resp = await api.setChatSessionModel(sid, model, entry?.provider);
        setModelSelections((prev) => ({ ...prev, [sid]: resp.model }));
        setSessions((prev) => prev.map((s) => (s.id === sid ? { ...s, model: resp.model } : s)));
      } catch (e) {
        const msg = e instanceof ApiError ? `[${e.code}] ${e.message}` : e instanceof Error ? e.message : String(e);
        setError(`模型切换失败：${msg}`);
        // 回滚到会话原模型。
        const s = sessions.find((x) => x.id === sid);
        setModelSelections((prev) => ({ ...prev, [sid]: s?.model ?? modelSelections.__default ?? "" }));
      }
    },
    [modelSelections, sessions, modelOptions],
  );

  const disabled = chatInputDisabled(activeState) || activeBusy || !activeId || busyAction;
  const lastMsg = activeState.messages[activeState.messages.length - 1];
  // 已点过停止（末条为本地"已停止"行）：隐藏停止按钮，避免 inert 按钮误导。
  const stopIssued = Boolean(lastMsg?.interrupted);
  const activeModel =
    modelSelections[activeId ?? ""] ??
    sessions.find((s) => s.id === activeId)?.model ??
    modelSelections.__default ??
    "";
  // 批五十一：下拉按 provider 分组（custom:<slug> / provider 名 / 默认）。
  const groupedModels = useMemo(() => {
    const groups = new Map<string, ChatModelOption[]>();
    for (const m of modelOptions) {
      const g = m.provider || "默认";
      const list = groups.get(g);
      if (list) list.push(m);
      else groups.set(g, [m]);
    }
    return Array.from(groups.entries());
  }, [modelOptions]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* 工具条：会话管理 */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-2">
          <TerminalSquare className="size-5 text-[var(--vigil-muted)]" />
          <h1 className="text-lg font-semibold">Chat</h1>
          <span className="hidden text-xs text-[var(--vigil-muted)] md:inline">· 和 Vigil 对话，看 agent 干活</span>
        </div>

        <div className="ml-auto flex flex-wrap items-center gap-2">
          <select
            value={activeId ?? ""}
            onChange={(e) => switchSession(e.target.value)}
            title="活会话切换"
            className="h-8 max-w-[220px] rounded border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-2 font-mono text-xs text-[var(--vigil-text)] outline-none"
          >
            {sessions.length === 0 && <option value="">（无会话）</option>}
            {sessions.map((s) => (
              <option key={s.id} value={s.id}>
                {s.title || s.id}
              </option>
            ))}
          </select>
          {activeUsage && (
            <span
              title={
                usagePct !== null
                  ? `context 已用 ${usagePct}%${usageHot ? "，接近上限建议新开会话" : ""}`
                  : "context 用量未知"
              }
              className={`inline-flex h-8 items-center rounded border px-2 font-mono text-[11px] ${
                usageHot
                  ? "border-orange-500/60 bg-orange-500/10 text-orange-600 dark:text-orange-400"
                  : "border-[var(--vigil-border)] bg-[var(--vigil-card)] text-[var(--vigil-muted)]"
              }`}
            >
              {contextUsageLabel(activeUsage)}
            </span>
          )}
          {usageHot && (
            <button
              type="button"
              onClick={() => void createSession(modelSelections[activeId ?? ""] ?? undefined)}
              disabled={busyAction}
              title="context 接近上限，建议新开会话"
              className="vigil-btn h-8 whitespace-nowrap border border-orange-500/60 bg-orange-500/10 text-xs text-orange-600 hover:bg-orange-500/20 dark:text-orange-400"
            >
              <MessageSquarePlus className="size-3.5" /> 建议新开
            </button>
          )}
          <button
            type="button"
            onClick={() => setUsageOpen((v) => !v)}
            title="会话 token 用量（当前会话实时 + 今天/近 30 天累计）"
            aria-pressed={usageOpen}
            className={`vigil-btn h-8 whitespace-nowrap border border-[var(--vigil-border)] text-xs ${
              usageOpen ? "bg-[var(--vigil-border)]/25" : ""
            }`}
          >
            <Coins className="size-3.5" /> 用量
          </button>
          <button
            type="button"
            onClick={() => void createSession(modelSelections[activeId ?? ""] ?? undefined)}
            disabled={busyAction}
            className="vigil-btn h-8 whitespace-nowrap border border-[var(--vigil-border)] text-xs"
          >
            <MessageSquarePlus className="size-3.5" /> 新建会话
          </button>
        </div>
      </div>

      {usageOpen && (
        <UsagePanel
          activeId={activeId}
          usage={usageData}
          usageLoading={usageLoading}
          usageError={usageError}
          analytics={analytics}
          analyticsLoading={analyticsLoading}
        />
      )}

      {error && (
        <div className="mb-2 flex items-center gap-2 rounded-md border border-red-500/50 bg-red-500/10 px-3 py-2 text-xs text-red-600 dark:text-red-400">
          <XCircle className="size-4 shrink-0" />
          <span className="min-w-0 flex-1">{error}</span>
          <button type="button" onClick={() => setError(null)} aria-label="关闭错误提示" className="text-[var(--vigil-muted)] hover:text-[var(--vigil-text)]">
            <X className="size-3.5" />
          </button>
        </div>
      )}

      {stopWarning && (
        <div className="mb-2 flex items-center gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-xs text-amber-600 dark:text-amber-400">
          <ShieldAlert className="size-4 shrink-0" />
          <span className="min-w-0 flex-1">{stopWarning}</span>
          <button
            type="button"
            onClick={() => {
              const sid = activeId;
              if (sid) void verifyBusyCleared(sid, setStates, setStopWarning, setBusyMap);
            }}
            className="shrink-0 underline"
          >
            重试
          </button>
        </div>
      )}

      {activeState.contextWarning && (
        <div className="mb-2 flex items-center gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-xs text-amber-600 dark:text-amber-400">
          <ShieldAlert className="size-4 shrink-0" />
          <span className="min-w-0 flex-1">{activeState.contextWarning}</span>
          <button
            type="button"
            onClick={() => void createSession(modelSelections[activeId ?? ""] ?? undefined)}
            disabled={busyAction}
            className="shrink-0 underline"
          >
            新开会话
          </button>
        </div>
      )}

      {/* 消息列表 */}
      <div className="scroll-thin min-h-0 flex-1 space-y-3 overflow-y-auto rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-bg)] p-3">
        {activeState.messages.length === 0 && (
          <div className="flex h-full min-h-[200px] flex-col items-center justify-center gap-2 text-sm text-[var(--vigil-muted)]">
            <Bot className="size-8 opacity-60" />
            {activeBusy ? (
              <span className="flex items-center gap-2">
                <Loader2 className="size-4 animate-spin" /> agent 处理中…
              </span>
            ) : activeId ? (
              "发一条消息开始对话"
            ) : (
              "创建会话后开始对话"
            )}
          </div>
        )}
        {activeState.messages.map((m) => (
          <MessageBubble
            key={m.id}
            msg={m}
            onToggleTool={toggleTool}
            onResolveApproval={resolveApproval}
            onResolveClarify={resolveClarify}
          />
        ))}
        <div ref={bottomRef} />
      </div>

      {/* 输入行 */}
      <form onSubmit={(e) => void send(e)} className="mt-2 shrink-0">
        {modelOptions.length > 0 && activeId && (
          <div className="mb-1.5 flex items-center gap-2">
            <span className="text-[10px] text-[var(--vigil-muted)]">模型</span>
            <select
              value={activeModel}
              onChange={(e) => void changeSessionModel(activeId, e.target.value)}
              disabled={activeBusy || busyAction}
              title="会话模型（新消息生效）"
              aria-label="会话模型"
              className="h-7 max-w-[320px] rounded border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-2 font-mono text-[11px] text-[var(--vigil-text)] outline-none disabled:opacity-60"
            >
              {groupedModels.map(([group, items]) => (
                <optgroup key={group} label={group}>
                  {items.map((m) => (
                    <option key={`${group}::${m.id}`} value={m.id}>
                      {m.name}
                      {m.default ? " · 默认" : ""}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            <span className="min-w-0 flex-1 truncate text-[10px] text-[var(--vigil-muted)]">
              {modelOptions.find((m) => m.id === activeModel)?.description || ""}
            </span>
          </div>
        )}
        <div className="flex items-center gap-2 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-3 py-2">
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={disabled ? (busyAction ? "正在创建会话…" : "agent 处理中，请稍候…") : "和 Vigil 说点什么，如「看下拓扑有几台主机」"}
            disabled={disabled}
            spellCheck={false}
            className="h-9 min-w-0 flex-1 bg-transparent text-sm text-[var(--vigil-text)] outline-none placeholder:text-[var(--vigil-muted)]/60 disabled:opacity-60"
          />
          {(activeBusy || chatInputDisabled(activeState)) && (
            <span className="hidden shrink-0 items-center gap-1.5 text-xs text-[var(--vigil-muted)] sm:inline-flex">
              <Loader2 className="size-3.5 animate-spin" /> agent 处理中…
            </span>
          )}
          {activeBusy && !busyAction && !stopIssued && (
            <StopButton stopping={stopping} onStop={() => void stopTurn()} />
          )}
          <button
            type="submit"
            disabled={disabled || !draft.trim()}
            aria-label="发送"
            className="vigil-btn vigil-btn-primary h-8 shrink-0 px-3 text-sm"
          >
            <Send className="size-3.5" /> 发送
          </button>
        </div>
      </form>
    </div>
  );
}

/** 有效 busy = 本地槽位 busy 或注册表快照 busy（§7：指示不丢失）。 */
function activeBusyOf(
  activeId: string | null,
  states: Record<string, ChatTurnState>,
  busyMap: Record<string, boolean>,
): boolean {
  if (!activeId) return false;
  return Boolean((states[activeId] && states[activeId].busy) || busyMap[activeId]);
}
