import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { Play, ShieldAlert, XCircle, Clock } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import type { ExecResponse, SessionSummary } from "@/lib/api";
import { isMockEnabled, mockExecStream, mockRunExec, MOCK_SESSIONS } from "@/lib/mock";
import { cn } from "@/lib/ops";

/**
 * Agent Terminal 执行控制台（批二十八契约）：
 * POST /api/exec → executed / needs_approval / denied 三态；
 * executed 时订阅 SSE stream（exec:output → exec:exit）；
 * needs_approval 弹审批卡（approve once / deny 内联 + 跳审批中心）。
 * mock 模式（localStorage vigil-mock=1）用文档 IP 占位数据。
 */
export function ExecConsole({
  expanded = false,
}: {
  /** true = Terminal 页大视图；false = 底部面板紧凑形态。 */
  expanded?: boolean;
}) {
  const navigate = useNavigate();
  const mock = isMockEnabled();

  const [command, setCommand] = useState("");
  const [env, setEnv] = useState("");
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionId, setSessionId] = useState<string>("");

  const [busy, setBusy] = useState(false);
  const [output, setOutput] = useState<string[]>([]);
  const [approval, setApproval] = useState<ExecResponse | null>(null);
  const [denied, setDenied] = useState<ExecResponse | null>(null);
  const [execError, setExecError] = useState<string | null>(null);
  const [approving, setApproving] = useState(false);
  const [mockNote] = useState(mock);
  const abortRef = useRef<AbortController | null>(null);
  const outputRef = useRef<HTMLDivElement>(null);

  // 会话列表（GET /api/sessions；未就绪 → mock 占位）
  useEffect(() => {
    let alive = true;
    api
      .getSessions({ limit: 20 })
      .then((resp) => {
        if (!alive) return;
        if (resp.sessions && resp.sessions.length > 0) {
          setSessions(resp.sessions);
          setSessionId(resp.sessions[0].session_id);
        } else if (mock) {
          setSessions(MOCK_SESSIONS);
          setSessionId(MOCK_SESSIONS[0].session_id);
        }
      })
      .catch(() => {
        if (alive && mock) {
          setSessions(MOCK_SESSIONS);
          setSessionId(MOCK_SESSIONS[0].session_id);
        }
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const reset = () => {
    setOutput([]);
    setApproval(null);
    setDenied(null);
    setExecError(null);
  };

  const run = useCallback(async () => {
    const cmd = command.trim();
    if (!cmd || busy) return;
    reset();
    setBusy(true);
    try {
      const resp = mock
        ? mockRunExec(cmd)
        : await api.runExec({ command: cmd, env: env || undefined, session_id: sessionId || undefined });

      if (resp.status === "executed") {
        setOutput((prev) => [...prev, `$ ${cmd}`, resp.output ?? ""]);
        // SSE 流（mock 或真实；失败静默，输出已含）
        if (resp.exec_id) {
          const ctrl = new AbortController();
          abortRef.current = ctrl;
          const handler = mock
            ? () => mockExecStream(resp.exec_id!, (ev) => handleStreamEvent(ev), ctrl.signal)
            : () => api.execStream(resp.exec_id!, (ev) => handleStreamEvent(ev), ctrl.signal);
          handler().catch(() => {});
        }
      } else if (resp.status === "needs_approval") {
        setApproval(resp);
      } else if (resp.status === "denied") {
        setDenied(resp);
      } else {
        setExecError(resp.reason ?? "未知执行状态");
      }
    } catch (e) {
      if (e instanceof ApiError) {
        setExecError(`[${e.code}] ${e.message}`);
      } else {
        setExecError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setBusy(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [command, busy, env, sessionId, mock]);

  const handleStreamEvent = useCallback((ev: { type: string; data: unknown }) => {
    if (ev.type === "exec:output") {
      const chunk = (ev.data as { chunk?: string })?.chunk ?? "";
      if (chunk) setOutput((prev) => [...prev, chunk]);
    } else if (ev.type === "exec:exit") {
      const code = (ev.data as { exit_code?: number })?.exit_code;
      setOutput((prev) => [...prev, `[exit ${code ?? "?"}]`]);
    } else if (ev.type === "exec:error") {
      setOutput((prev) => [...prev, `[stream error] ${String(ev.data)}`]);
    }
  }, []);

  useEffect(() => {
    outputRef.current?.scrollTo({ top: outputRef.current.scrollHeight });
  }, [output]);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const approveOnce = async () => {
    if (!approval?.approval_id) return;
    setApproving(true);
    try {
      if (mock) {
        setApproval(null);
        setOutput((prev) => [...prev, `[approval ${approval.approval_id} approved (once) — 模拟]`]);
      } else {
        await api.approveApproval(approval.approval_id, "once");
        setApproval(null);
        setOutput((prev) => [...prev, `[approval ${approval.approval_id} approved (once)]`]);
      }
    } catch (e) {
      setExecError(e instanceof Error ? e.message : String(e));
    } finally {
      setApproving(false);
    }
  };

  const deny = async () => {
    if (!approval?.approval_id) return;
    setApproving(true);
    try {
      if (mock) {
        setApproval(null);
        setDenied({ status: "denied", code: "denied", reason: "用户拒绝（模拟）" });
      } else {
        await api.denyApproval(approval.approval_id);
        setApproval(null);
        setDenied({ status: "denied", code: "denied", reason: "用户拒绝" });
      }
    } catch (e) {
      setExecError(e instanceof Error ? e.message : String(e));
    } finally {
      setApproving(false);
    }
  };

  return (
    <div className={cn("flex h-full min-h-0 flex-col", expanded ? "gap-3" : "gap-2")}>
      {mockNote && (
        <div className="inline-flex w-fit items-center gap-1.5 rounded border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[11px] text-amber-600 dark:text-amber-400">
          <Clock className="size-3" /> 模拟数据（localStorage vigil-mock=1，文档 IP 占位）
        </div>
      )}

      {/* 输入行 */}
      <div className="flex items-center gap-2">
        <input
          value={command}
          onChange={(e) => setCommand(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") run();
          }}
          placeholder="$ 输入命令（如 kubectl get pods）…"
          className="vigil-input h-8 flex-1"
          spellCheck={false}
        />
        <select
          value={env}
          onChange={(e) => setEnv(e.target.value)}
          title="env（缺省 = 当前会话 env）"
          className="vigil-input h-8 w-24 text-xs"
        >
          <option value="">env 自动</option>
          <option value="local">local</option>
          <option value="test">test</option>
          <option value="dev">dev</option>
          <option value="prod">prod</option>
        </select>
        {sessions.length > 0 && (
          <select
            value={sessionId}
            onChange={(e) => setSessionId(e.target.value)}
            title="会话（session_id 上下文）"
            className="vigil-input h-8 w-44 text-xs"
          >
            {sessions.map((s) => (
              <option key={s.session_id} value={s.session_id}>
                {s.title ?? s.session_id}
              </option>
            ))}
          </select>
        )}
        <button
          type="button"
          onClick={run}
          disabled={busy || !command.trim()}
          className="vigil-btn vigil-btn-primary h-8"
        >
          <Play className="size-3.5" /> {busy ? "执行中…" : "执行"}
        </button>
      </div>

      {/* needs_approval 审批卡 */}
      {approval && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-xs">
          <ShieldAlert className="size-4 shrink-0 text-amber-500" />
          <span className="font-medium">命令需审批</span>
          <span className="font-mono text-[var(--vigil-muted)]">
            {approval.approval_id ?? approval.exec_id}
          </span>
          <span className="min-w-0 flex-1 truncate text-[var(--vigil-text)] opacity-80">
            {command}
          </span>
          <button type="button" onClick={approveOnce} disabled={approving} className="vigil-btn vigil-btn-primary h-6 px-2 text-xs">
            批准（once）
          </button>
          <button type="button" onClick={deny} disabled={approving} className="vigil-btn h-6 border border-[var(--vigil-border)] px-2 text-xs">
            拒绝
          </button>
          <button type="button" onClick={() => navigate("/approvals")} className="vigil-link text-xs">
            审批中心 →
          </button>
        </div>
      )}

      {/* denied / error */}
      {denied && (
        <div className="flex items-center gap-2 rounded-md border border-red-500/50 bg-red-500/10 px-3 py-2 text-xs">
          <XCircle className="size-4 shrink-0 text-red-500" />
          <span className="font-medium">已拒绝</span>
          <span className="text-[var(--vigil-text)] opacity-80">{denied.reason ?? denied.code}</span>
        </div>
      )}
      {execError && (
        <div className="flex items-center gap-2 rounded-md border border-red-500/50 bg-red-500/10 px-3 py-2 text-xs">
          <XCircle className="size-4 shrink-0 text-red-500" />
          <span className="text-[var(--vigil-text)] opacity-80">{execError}</span>
        </div>
      )}

      {/* 输出区 */}
      <div
        ref={outputRef}
        className="scroll-thin min-h-0 flex-1 overflow-y-auto rounded-md border border-white/5 bg-black/20 p-3 font-mono text-xs leading-relaxed"
      >
        {output.length === 0 ? (
          <div className="flex h-full min-h-[120px] flex-col items-start justify-center gap-1 opacity-60">
            <div className="flex items-center gap-2">
              <Play className="size-3.5" />
              <span>执行 API 已接线（POST /api/exec + SSE）；后端批二十八落地后在此输出。</span>
            </div>
          </div>
        ) : (
          output.map((line, i) => (
            <div key={i} className={cn("whitespace-pre-wrap break-all", line.startsWith("$ ") && "text-sky-400")}>
              {line}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
