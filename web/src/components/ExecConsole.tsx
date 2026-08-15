import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { Play, ShieldAlert, XCircle, Clock } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import type { ExecResponse, SessionSummary } from "@/lib/api";
import { isMockEnabled, mockExecStream, mockRunExec, MOCK_SESSIONS } from "@/lib/mock";
import { cn } from "@/lib/ops";

/**
 * Agent Terminal 执行控制台：
 * 输入行为真实终端样式——`$` 提示符 + 全宽命令输入 + 紧凑 env/会话选择 +
 * 横排「执行」按钮；输出区在上（monospace，`$` 命令高亮），输入行在下。
 * POST /api/exec 三态：executed（输出 + SSE 流）/ needs_approval（弹审批卡）/
 * denied（拒绝原因）。
 */
export function ExecConsole() {
  const navigate = useNavigate();
  const mock = isMockEnabled();

  const [command, setCommand] = useState("");
  const [env, setEnv] = useState("");
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionId, setSessionId] = useState("");

  const [busy, setBusy] = useState(false);
  const [output, setOutput] = useState<string[]>([]);
  const [approval, setApproval] = useState<ExecResponse | null>(null);
  const [denied, setDenied] = useState<ExecResponse | null>(null);
  const [execError, setExecError] = useState<string | null>(null);
  const [approving, setApproving] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const outputRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // 会话列表（未就绪时留空，mock 模式用占位）
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

  const run = useCallback(async () => {
    const cmd = command.trim();
    if (!cmd || busy) return;
    setOutput([]);
    setApproval(null);
    setDenied(null);
    setExecError(null);
    setBusy(true);
    try {
      const resp = mock
        ? mockRunExec(cmd)
        : await api.runExec({ command: cmd, env: env || undefined, session_id: sessionId || undefined });

      if (resp.status === "executed") {
        setOutput((prev) => [...prev, `$ ${cmd}`, resp.output ?? ""]);
        if (resp.exec_id) {
          const ctrl = new AbortController();
          abortRef.current = ctrl;
          const handler = mock
            ? () => mockExecStream(resp.exec_id!, handleStreamEvent, ctrl.signal)
            : () => api.execStream(resp.exec_id!, handleStreamEvent, ctrl.signal);
          handler().catch(() => {});
        }
      } else if (resp.status === "needs_approval") {
        setApproval(resp);
      } else if (resp.status === "denied") {
        setDenied(resp);
      } else {
        // 后端错误字段是 error（对象），reason 兜底——超时等原因不再被吞。
        setExecError(resp.reason ?? resp.error?.message ?? "未知执行状态");
      }
    } catch (e) {
      if (e instanceof ApiError) {
        setExecError(e.message);
      } else {
        setExecError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setBusy(false);
      inputRef.current?.focus();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [command, busy, env, sessionId, mock, handleStreamEvent]);

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
        setOutput((prev) => [...prev, `[已批准 ${approval.approval_id}（模拟数据）]`]);
      } else {
        await api.approveApproval(approval.approval_id, "once");
        setApproval(null);
        setOutput((prev) => [...prev, `[已批准 ${approval.approval_id}]`]);
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
        setDenied({ status: "denied", code: "denied", reason: "已拒绝该命令（模拟数据）" });
      } else {
        await api.denyApproval(approval.approval_id);
        setApproval(null);
        setDenied({ status: "denied", code: "denied", reason: "已拒绝该命令" });
      }
    } catch (e) {
      setExecError(e instanceof Error ? e.message : String(e));
    } finally {
      setApproving(false);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* 输出区（滚动日志，monospace，$ 命令高亮） */}
      <div
        ref={outputRef}
        className="scroll-thin min-h-0 flex-1 overflow-y-auto rounded-md border border-white/5 bg-black/20 p-3 font-mono text-xs leading-relaxed"
      >
        {output.length === 0 ? (
          <div className="flex h-full min-h-[140px] flex-col items-start justify-center gap-1 opacity-70">
            <span className="flex items-center gap-2">
              <span className="text-sky-400">$</span>
              <span>输入命令开始执行</span>
            </span>
          </div>
        ) : (
          output.map((line, i) => (
            <div
              key={i}
              className={cn("whitespace-pre-wrap break-words", line.startsWith("$ ") && "text-sky-400")}
            >
              {line}
            </div>
          ))
        )}
      </div>

      {/* needs_approval 审批卡（不遮挡输出区） */}
      {approval && (
        <div className="flex shrink-0 flex-wrap items-center gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-xs">
          <ShieldAlert className="size-4 shrink-0 text-amber-500" />
          <span className="font-medium">该命令需要审批</span>
          <span className="font-mono opacity-70">{approval.approval_id ?? approval.exec_id}</span>
          <span className="min-w-0 flex-1 truncate opacity-80">{command}</span>
          <button type="button" onClick={approveOnce} disabled={approving} className="vigil-btn vigil-btn-primary h-7 whitespace-nowrap px-2 text-xs">
            批准
          </button>
          <button type="button" onClick={deny} disabled={approving} className="vigil-btn h-7 whitespace-nowrap border border-[var(--vigil-border)] px-2 text-xs">
            拒绝
          </button>
          <button type="button" onClick={() => navigate("/approvals")} className="vigil-link whitespace-nowrap text-xs">
            查看审批中心 →
          </button>
        </div>
      )}

      {/* 拒绝 / 错误 */}
      {(denied || execError) && (
        <div className="flex shrink-0 items-center gap-2 rounded-md border border-red-500/50 bg-red-500/10 px-3 py-2 text-xs">
          <XCircle className="size-4 shrink-0 text-red-500" />
          <span>{denied ? (denied.reason ?? "该命令被拒绝") : execError}</span>
        </div>
      )}

      {/* 输入行：$ 提示符 + 全宽命令 + 紧凑 env/会话 + 横排执行按钮 */}
      <div className="flex shrink-0 items-center gap-2 rounded-md border border-white/10 bg-black/20 px-3 py-2 font-mono">
        <span className="shrink-0 text-base text-sky-400">$</span>
        <input
          ref={inputRef}
          value={command}
          onChange={(e) => setCommand(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") run();
          }}
          placeholder="输入命令，如 kubectl get nodes"
          spellCheck={false}
          className="h-9 min-w-0 flex-1 bg-transparent font-mono text-sm text-inherit outline-none placeholder:opacity-40"
        />
        <select
          value={env}
          onChange={(e) => setEnv(e.target.value)}
          title="执行环境"
          className="h-8 w-20 shrink-0 rounded border border-white/15 bg-black/30 px-1 font-mono text-xs outline-none"
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
            title="当前会话"
            className="hidden h-8 w-40 shrink-0 rounded border border-white/15 bg-black/30 px-1 font-mono text-[11px] outline-none lg:block"
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
          className="vigil-btn vigil-btn-primary h-8 shrink-0 whitespace-nowrap text-sm"
        >
          <Play className="size-3.5" />
          {busy ? "执行中" : "执行"}
        </button>
      </div>

      {mock && (
        <div className="mt-1 inline-flex w-fit items-center gap-1.5 rounded border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[11px] text-amber-600 dark:text-amber-400">
          <Clock className="size-3" /> 模拟数据
        </div>
      )}
    </div>
  );
}
