import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
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
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
import type { ChatSessionSummary } from "@/lib/api";
import {
  applyChatEvent,
  chatInputDisabled,
  createChatState,
  markApprovalResolved,
  markTurnInterrupted,
  pushUserMessage,
  stateFromHistory,
  toggleToolExpanded,
  type ChatApprovalCard,
  type ChatMessage,
  type ChatTurnState,
} from "@/lib/chat";
import { Markdown } from "@/components/Markdown";
import StopButton from "@/components/StopButton";
import { cn } from "@/lib/ops";

/**
 * 对话 Session 页（批三十一，UI 壳核心价值页）：
 * POST /api/chat/sessions + GET /api/chat/sessions + POST
 * /api/chat/sessions/{id}/messages（SSE：chat:delta/tool/tool_result/
 * approval_pending/done/error）。流式打字机渲染；工具调用折叠行；审批卡弹
 * 在消息流内（走现有 POST /api/approvals/{id}/approve|deny）；同会话串行，
 * agent 忙时输入禁用。
 */

function ToolRow({
  tool,
  onToggle,
}: {
  tool: ChatMessage["tools"][number];
  onToggle: () => void;
}) {
  return (
    <div className="overflow-hidden rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)]">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={tool.expanded}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-xs hover:bg-black/5 dark:hover:bg-white/5"
      >
        {tool.expanded ? (
          <ChevronDown className="size-3.5 shrink-0 text-[var(--vigil-muted)]" />
        ) : (
          <ChevronRight className="size-3.5 shrink-0 text-[var(--vigil-muted)]" />
        )}
        <Wrench className="size-3.5 shrink-0 text-sky-500" />
        <span className="font-mono font-medium">{tool.name}</span>
        {tool.ok !== undefined && (
          <span
            className={cn(
              "ml-auto shrink-0 text-[10px]",
              tool.ok ? "text-[var(--vigil-ok)]" : "text-[var(--vigil-error)]",
            )}
          >
            {tool.ok ? "✓" : "✗"}
          </span>
        )}
      </button>
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
              <pre className="scroll-thin whitespace-pre-wrap break-words font-mono text-[11px] text-[var(--vigil-text)] opacity-85">
                {tool.outputSummary || "（空输出）"}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ApprovalCard({
  card,
  onResolve,
}: {
  card: ChatApprovalCard;
  onResolve: (card: ChatApprovalCard, status: "approved" | "denied") => void;
}) {
  const busy = card.status === "approved" || card.status === "denied";
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-xs",
        card.status === "approved"
          ? "border-emerald-500/50 bg-emerald-500/10"
          : card.status === "denied"
            ? "border-red-500/50 bg-red-500/10"
            : "border-amber-500/50 bg-amber-500/10",
      )}
    >
      <ShieldAlert className={cn("size-4 shrink-0", card.status === "approved" ? "text-emerald-500" : card.status === "denied" ? "text-red-500" : "text-amber-500")} />
      <span className="font-medium">该命令需要审批</span>
      {card.grade && <span className="rounded bg-amber-500/15 px-1.5 py-px text-[10px] text-amber-600 dark:text-amber-400">L{card.grade}</span>}
      {card.env && <span className="text-[var(--vigil-muted)]">{card.env}</span>}
      <span className="min-w-0 flex-1 truncate font-mono">{card.command}</span>
      {card.status === "pending" && (
        <>
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
        </>
      )}
      {card.status === "approved" && <span className="text-emerald-500">已批准</span>}
      {card.status === "denied" && <span className="text-red-500">已拒绝</span>}
    </div>
  );
}

function MessageBubble({ msg, onToggleTool, onResolveApproval }: {
  msg: ChatMessage;
  onToggleTool: (toolId: number) => void;
  onResolveApproval: (card: ChatApprovalCard, status: "approved" | "denied") => void;
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
        {msg.content ? <Markdown text={msg.content} /> : null}
        {msg.tools.length > 0 && (
          <div className="mt-2 space-y-1.5">
            {msg.tools.map((t) => (
              <ToolRow key={t.id} tool={t} onToggle={() => onToggleTool(t.id)} />
            ))}
          </div>
        )}
        {msg.approvals.length > 0 && (
          <div className="mt-2 space-y-1.5">
            {msg.approvals.map((a) => (
              <ApprovalCard key={a.approvalId} card={a} onResolve={onResolveApproval} />
            ))}
          </div>
        )}
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
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState(false);
  const [stopping, setStopping] = useState(false);
  const abortRefs = useRef<Record<string, AbortController>>({});
  const loadedRef = useRef<Set<string>>(new Set());
  const bottomRef = useRef<HTMLDivElement>(null);

  const activeState = (activeId && states[activeId]) || createChatState();

  const refreshSessions = useCallback(async () => {
    try {
      const resp = await api.listChatSessions();
      if (resp.error) return;
      setSessions(resp.sessions ?? []);
      return resp.sessions ?? [];
    } catch {
      return [];
    }
  }, []);

  const loadSessionHistory = useCallback(async (id: string) => {
    if (loadedRef.current.has(id)) return;
    loadedRef.current.add(id);
    try {
      const resp = await api.getChatHistory(id);
      setStates((prev) => {
        // 槽位已有消息（如拉取期间用户已发消息/流式渲染中）→ 不覆盖现场。
        const existing = prev[id];
        if (existing && existing.messages.length > 0) return prev;
        return { ...prev, [id]: stateFromHistory(resp.messages ?? [], Boolean(resp.busy)) };
      });
    } catch {
      // 拉取失败：允许重试（下次切回再试），空状态也能发消息。
      loadedRef.current.delete(id);
    }
  }, []);

  const createSession = useCallback(async () => {
    setError(null);
    setBusyAction(true);
    try {
      const resp = await api.createChatSession();
      const sid = resp.chat_session_id;
      setActiveId(sid);
      loadedRef.current.add(sid); // 新会话无历史，跳过 GET
      setStates((prev) => ({ ...prev, [sid]: createChatState() }));
      await refreshSessions();
    } catch (e) {
      setError(e instanceof ApiError ? `[${e.code}] ${e.message}` : e instanceof Error ? e.message : String(e));
    } finally {
      setBusyAction(false);
    }
  }, [refreshSessions]);

  // 初始化：列活会话；无则新建。
  useEffect(() => {
    let alive = true;
    api
      .listChatSessions()
      .then(async (resp) => {
        if (!alive) return;
        const list = resp.sessions ?? [];
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
  }, []);

  // 切到无缓存的会话 → 拉历史恢复现场（1a：切页/切回消息完整）。
  useEffect(() => {
    if (!activeId) return;
    void loadSessionHistory(activeId);
  }, [activeId, loadSessionHistory]);

  // 后台 turn 收尾轮询：当前会话 busy（切走被 abort 的流）时轮询注册表，
  // busy 翻转 → 重拉历史拿最终内容（1b 验收③：在跑的显示"处理中"，跑完恢复）。
  useEffect(() => {
    if (!activeId || !activeState.busy) return;
    const timer = window.setInterval(async () => {
      try {
        const resp = await api.listChatSessions();
        const s = (resp.sessions ?? []).find((x) => x.id === activeId);
        if (s && !s.busy) {
          const h = await api.getChatHistory(activeId);
          setStates((prev) => ({ ...prev, [activeId]: stateFromHistory(h.messages ?? [], false) }));
        }
      } catch {
        // transient network hiccup — next tick retries
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [activeId, activeState.busy]);

  // 新内容自动滚底。
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [activeState.messages]);

  const send = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      const text = draft.trim();
      const sid = activeId;
      const st = (sid && states[sid]) || createChatState();
      if (!text || !sid || chatInputDisabled(st)) return;
      setDraft("");
      setError(null);
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
    [activeId, draft, refreshSessions, states],
  );

  const stopTurn = useCallback(async () => {
    const sid = activeId;
    const st = (sid && states[sid]) || createChatState();
    if (!sid || !chatInputDisabled(st) || stopping) return;
    setStopping(true);
    setError(null);
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

  const switchSession = useCallback(
    (id: string) => {
      if (id === activeId) return;
      // 切走：abort 旧会话 SSE（后台 turn 继续跑，切回由轮询恢复现场）。
      if (activeId) abortRefs.current[activeId]?.abort();
      setActiveId(id);
      setError(null);
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

  const disabled = chatInputDisabled(activeState) || !activeId || busyAction;

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
          <button
            type="button"
            onClick={() => void createSession()}
            disabled={busyAction}
            className="vigil-btn h-8 whitespace-nowrap border border-[var(--vigil-border)] text-xs"
          >
            <MessageSquarePlus className="size-3.5" /> 新建会话
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-2 flex items-center gap-2 rounded-md border border-red-500/50 bg-red-500/10 px-3 py-2 text-xs text-red-600 dark:text-red-400">
          <XCircle className="size-4 shrink-0" />
          <span className="min-w-0 flex-1">{error}</span>
          <button type="button" onClick={() => setError(null)} aria-label="关闭错误提示" className="text-[var(--vigil-muted)] hover:text-[var(--vigil-text)]">
            <X className="size-3.5" />
          </button>
        </div>
      )}

      {/* 消息列表 */}
      <div className="scroll-thin min-h-0 flex-1 space-y-3 overflow-y-auto rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-bg)] p-3">
        {activeState.messages.length === 0 && (
          <div className="flex h-full min-h-[200px] flex-col items-center justify-center gap-2 text-sm text-[var(--vigil-muted)]">
            <Bot className="size-8 opacity-60" />
            {activeId ? "发一条消息开始对话" : "创建会话后开始对话"}
          </div>
        )}
        {activeState.messages.map((m) => (
          <MessageBubble
            key={m.id}
            msg={m}
            onToggleTool={toggleTool}
            onResolveApproval={resolveApproval}
          />
        ))}
        <div ref={bottomRef} />
      </div>

      {/* 输入行 */}
      <form onSubmit={(e) => void send(e)} className="mt-2 shrink-0">
        <div className="flex items-center gap-2 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-3 py-2">
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={disabled ? (busyAction ? "正在创建会话…" : "agent 处理中，请稍候…") : "和 Vigil 说点什么，如「看下拓扑有几台主机」"}
            disabled={disabled}
            spellCheck={false}
            className="h-9 min-w-0 flex-1 bg-transparent text-sm text-[var(--vigil-text)] outline-none placeholder:text-[var(--vigil-muted)]/60 disabled:opacity-60"
          />
          {chatInputDisabled(activeState) && (
            <span className="hidden shrink-0 items-center gap-1.5 text-xs text-[var(--vigil-muted)] sm:inline-flex">
              <Loader2 className="size-3.5 animate-spin" /> agent 思考中…
            </span>
          )}
          {chatInputDisabled(activeState) && !busyAction && (
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

