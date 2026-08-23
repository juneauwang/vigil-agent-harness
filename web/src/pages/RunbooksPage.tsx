import { Fragment, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import {
  BookOpen,
  ChevronDown,
  ChevronRight,
  Clock,
  FileText,
  ListChecks,
  Loader2,
  Lock,
  Play,
  Terminal,
  TriangleAlert,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  RunbookCoverageResponse,
  RunbookDetailResponse,
  RunbookExecution,
  RunbookLock,
  RunbookProgressEvent,
  RunbookRunningExec,
  RunbookStepResult,
  RunbookSummary,
} from "@/lib/api";
import { DetailTree } from "@/components/DetailTree";
import { EmptyState } from "@/components/EmptyState";
import { cn, yamlPreview } from "@/lib/ops";

function EnvTag({ env }: { env?: string }) {
  if (!env) return null;
  const colors: Record<string, string> = {
    prod: "env-prod",
    test: "env-test",
    uat: "env-dev",
    dev: "env-dev",
    local: "env-local",
  };
  return <span className={cn("vigil-env", colors[env] ?? "env-other")}>{env}</span>;
}

function KindTag({ kind, checklist }: { kind?: string; checklist?: boolean }) {
  const label =
    kind === "deploy" ? "部署"
    : kind === "incident" ? "事故"
    : kind === "maintenance" ? "维护"
    : kind === "checklist" ? "清单"
    : kind ?? "runbook";
  return (
    <span className="rounded border border-[var(--vigil-border)] px-1.5 py-px text-[10px] text-[var(--vigil-muted)]">
      {label}
      {checklist ? " · checklist" : ""}
    </span>
  );
}

function ActionBadge({ action }: { action?: string }) {
  if (!action) return null;
  const family =
    ["start", "stop", "restart", "reload", "enable", "disable"].includes(action) ? "生命周期"
    : ["reboot", "shutdown"].includes(action) ? "主机"
    : ["deploy", "rollback", "scale", "decommission"].includes(action) ? "发布"
    : ["backup", "restore"].includes(action) ? "数据"
    : action === "apply_config" ? "配置"
    : ["query", "fetch_log", "verify"].includes(action) ? "查询"
    : action === "transfer_file" ? "文件"
    : action === "run_script" ? "执行"
    : ["install", "upgrade", "remove"].includes(action) ? "包"
    : "";
  return (
    <span
      className="inline-flex items-center gap-1 rounded border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)] px-1.5 py-px font-mono text-[10px]"
      title={family ? `${family}族动作` : undefined}
    >
      {action}
      {family ? <span className="text-[var(--vigil-muted)]">· {family}</span> : null}
    </span>
  );
}

function ParamsView({ params }: { params: Record<string, unknown> }) {
  const entries = Object.entries(params ?? {});
  if (entries.length === 0) return null;
  return (
    <div className="mt-1.5 space-y-0.5 text-xs">
      {entries.map(([k, v]) => (
        <div key={k} className="flex gap-1.5">
          <span className="shrink-0 font-mono text-[var(--vigil-muted)]">{k}:</span>
          {typeof v === "object" && v !== null ? (
            <div className="min-w-0 flex-1">
              <DetailTree data={v as Record<string, unknown>} />
            </div>
          ) : (
            <code className="min-w-0 flex-1 break-all text-[var(--vigil-text)] opacity-80">
              {String(v)}
            </code>
          )}
        </div>
      ))}
    </div>
  );
}

function OnFailureTag({ value }: { value?: unknown }) {
  if (value === undefined || value === null) return null;
  const label =
    value === "stop" ? "失败即停"
    : value === "continue" ? "失败继续"
    : value === "rollback" ? "回滚"
    : typeof value === "object" && value !== null && "rollback" in (value as object)
      ? `回滚 → ${String((value as Record<string, unknown>).rollback)}`
      : String(value);
  return (
    <span className="inline-flex items-center gap-1 rounded border border-[var(--vigil-border)] px-1.5 py-px text-[10px] text-[var(--vigil-muted)]">
      <TriangleAlert className="size-3" />
      on_failure: {label}
    </span>
  );
}

function ExpectView({ expect }: { expect: Record<string, unknown> }) {
  if (!expect || typeof expect !== "object") return null;
  const predKeys = ["contains", "http_status", "body_contains", "exit_code"];
  return (
    <div className="mt-1.5 space-y-0.5 text-xs">
      <div className="text-[var(--vigil-muted)]">
        expect: 通道 <code className="text-[var(--vigil-text)] opacity-75">{String(expect.target ?? "")}</code>
      </div>
      {predKeys.filter((k) => k in expect).map((k) => (
        <div key={k} className="pl-3 text-[var(--vigil-muted)]">
          {k}:{" "}
          {typeof expect[k] === "object" && expect[k] !== null ? (
            <code className="text-[var(--vigil-text)] opacity-75">
              {JSON.stringify(expect[k])}
            </code>
          ) : (
            <code className="text-[var(--vigil-text)] opacity-75">{String(expect[k])}</code>
          )}
        </div>
      ))}
      {"url" in expect ? (
        <div className="pl-3 text-[var(--vigil-muted)]">
          url: <code className="text-[var(--vigil-text)] opacity-75">{String(expect.url)}</code>
        </div>
      ) : null}
    </div>
  );
}

function StepView({ step }: { step: Record<string, unknown> }) {
  const isV2 = typeof step.action === "string" && step.action.length > 0;
  return (
    <>
      <div className="mb-1.5 flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">{String(step.title ?? step.id ?? "")}</span>
        <span className="text-xs text-[var(--vigil-muted)]">id: {String(step.id ?? "")}</span>
        {isV2 ? <ActionBadge action={String(step.action)} /> : null}
        <OnFailureTag value={step.on_failure} />
      </div>
      {isV2 ? (
        <ParamsView params={(step.params as Record<string, unknown>) ?? {}} />
      ) : (
        Array.isArray(step.commands) && step.commands.length > 0 && (
          <div className="space-y-1">
            {step.commands.map((cmd, j) => (
              <pre
                key={j}
                className="scroll-thin overflow-x-auto rounded bg-[var(--vigil-muted-bg)] px-2.5 py-1.5 text-[11px]"
              >
                {String(cmd)}
              </pre>
            ))}
          </div>
        )
      )}
      {isV2 && step.expect ? (
        <ExpectView expect={step.expect as Record<string, unknown>} />
      ) : (
        (step.verify || step.expect) ? (
          <div className="mt-1.5 space-y-0.5 text-xs text-[var(--vigil-muted)]">
            {step.verify ? (
              <div>verify: <code className="text-[var(--vigil-text)] opacity-75">{String(step.verify)}</code></div>
            ) : null}
            {step.expect ? (
              <div>expect: <code className="text-[var(--vigil-text)] opacity-75">{String(step.expect)}</code></div>
            ) : null}
          </div>
        ) : null
      )}
    </>
  );
}

function YAMLPreview({ data }: { data: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const preview = yamlPreview(data);
  return (
    <div className="mt-3">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="vigil-btn gap-1 border border-[var(--vigil-border)] px-2 py-0.5 text-xs"
        aria-expanded={open}
      >
        {open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
        YAML 预览（已脱敏）
      </button>
      {open && (
        <pre className="scroll-thin mt-2 max-h-80 overflow-auto rounded-md border border-[var(--vigil-border)] p-3 text-[11px] leading-relaxed"
          style={{ background: "var(--vigil-terminal-bg)", color: "#cbd5e1" }}>
          {preview}
        </pre>
      )}
    </div>
  );
}

function RunbookDetail({ data }: { data: Record<string, unknown> }) {
  const steps = (data.steps as Array<Record<string, unknown>>) ?? [];
  const rollback = (data.rollback as Array<Record<string, unknown>>) ?? [];
  const triggers = (data.triggers as unknown[]) ?? [];
  const known = new Set([
    "name", "title", "version", "env", "kind", "checklist", "triggers",
    "summary", "steps", "rollback", "updated_at", "note",
    "clusters", "host_groups", "hosts", "schedule", "on_failure",
  ]);
  const schedule = data.schedule as Record<string, unknown> | undefined;
  const clusters = (data.clusters as string[] | undefined) ?? [];
  const hostGroups = (data.host_groups as string[] | undefined) ?? [];
  const scopeHosts = (data.hosts as string[] | undefined) ?? [];
  const scopeCount = clusters.length + hostGroups.length + scopeHosts.length;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-[var(--vigil-muted)]">
        <span>name: <span className="text-[var(--vigil-text)] opacity-80">{String(data.name ?? "")}</span></span>
        <span>schema: <span className="text-[var(--vigil-text)] opacity-80">
          v{String(data.version ?? "?")}{String(data.version) === "2" ? "（声明式动作）" : ""}
        </span></span>
        <span>env: <span className="text-[var(--vigil-text)] opacity-80">{String(data.env ?? "-")}</span></span>
        <span>kind: <span className="text-[var(--vigil-text)] opacity-80">{String(data.kind ?? "-")}</span></span>
        <span>更新: <span className="text-[var(--vigil-text)] opacity-80">{String(data.updated_at ?? "-")}</span></span>
      </div>

      {data.summary ? <p className="text-sm text-[var(--vigil-text)] opacity-80">{String(data.summary)}</p> : null}

      <div className="flex flex-wrap items-center gap-2">
        <OnFailureTag value={data.on_failure} />
        {schedule ? (
          <span className="inline-flex items-center gap-1 rounded border border-[var(--vigil-border)] px-1.5 py-px text-[10px] text-[var(--vigil-muted)]">
            <Clock className="size-3" />
            schedule: <code>{String(schedule.cron ?? "")}</code>
            {schedule.timezone ? <span>（{String(schedule.timezone)}）</span> : null}
          </span>
        ) : null}
        {scopeCount > 0 ? (
          <span className="inline-flex items-center gap-1 rounded border border-[var(--vigil-border)] px-1.5 py-px text-[10px] text-[var(--vigil-muted)]">
            目标范围: {clusters.length ? `集群 ${clusters.join(", ")}` : ""}
            {hostGroups.length ? `${clusters.length ? " · " : ""}主机组 ${hostGroups.join(", ")}` : ""}
            {scopeHosts.length ? `${clusters.length || hostGroups.length ? " · " : ""}主机 ${scopeHosts.join(", ")}` : ""}
          </span>
        ) : null}
      </div>

      {triggers.length > 0 && (
        <section>
          <h3 className="mb-2 flex items-center gap-2 text-xs font-semibold">
            <TriangleAlert className="size-3.5 text-[var(--vigil-muted)]" /> 触发条件
          </h3>
          <div className="flex flex-wrap gap-1.5">
            {triggers.map((t, i) => (
              <span key={i} className="rounded border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)] px-2 py-0.5 text-xs">
                {typeof t === "object" && t !== null ? JSON.stringify(t) : String(t)}
              </span>
            ))}
          </div>
        </section>
      )}

      {steps.length > 0 && (
        <section>
          <h3 className="mb-2 flex items-center gap-2 text-xs font-semibold">
            <ListChecks className="size-3.5 text-[var(--vigil-muted)]" /> 步骤（{steps.length}）
          </h3>
          <ol className="space-y-2">
            {steps.map((step, i) => (
              <li key={(step.id as string) ?? i} className="vigil-card flex items-start gap-2 p-3">
                <span className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full bg-[var(--vigil-muted-bg)] text-xs">
                  {i + 1}
                </span>
                <div className="min-w-0 flex-1">
                  <StepView step={step} />
                </div>
              </li>
            ))}
          </ol>
        </section>
      )}

      {rollback.length > 0 && (
        <section>
          <h3 className="mb-2 flex items-center gap-2 text-xs font-semibold">
            <Terminal className="size-3.5 text-[var(--vigil-muted)]" /> 回滚预案
          </h3>
          <div className="space-y-1.5">
            {rollback.map((rb, i) => (
              <div key={i} className="vigil-card p-3">
                <div className="mb-1 text-sm font-medium">
                  {String(rb.name ?? rb.title ?? `场景 ${i + 1}`)}
                </div>
                {Array.isArray(rb.steps) ? (
                  <div className="space-y-1.5">
                    {rb.steps.map((s, j) => (
                      <div key={j} className="rounded border border-dotted border-[var(--vigil-border)] px-2 py-1.5">
                        <StepView step={s as Record<string, unknown>} />
                      </div>
                    ))}
                  </div>
                ) : (
                  Array.isArray(rb.commands) &&
                    rb.commands.map((cmd, j) => (
                      <pre key={j} className="scroll-thin overflow-x-auto rounded bg-[var(--vigil-muted-bg)] px-2.5 py-1.5 text-[11px]">
                        {String(cmd)}
                      </pre>
                    ))
                )}
                {rb.note ? <div className="mt-1 text-xs text-[var(--vigil-muted)]">{String(rb.note)}</div> : null}
              </div>
            ))}
          </div>
        </section>
      )}

      {Object.keys(data).some((k) => !known.has(k)) && (
        <section>
          <h3 className="mb-2 text-xs font-semibold">其他字段</h3>
          <div className="vigil-card p-3">
            {Object.entries(data)
              .filter(([k]) => !known.has(k))
              .map(([k, v]) => (
                <div key={k} className="flex gap-2 border-b border-dotted border-[var(--vigil-border)] py-1.5 text-sm last:border-b-0">
                  <span className="min-w-32 shrink-0 text-[var(--vigil-muted)]">{k}</span>
                  <span className="min-w-0 flex-1">
                    {typeof v === "object" && v !== null ? (
                      <DetailTree data={v as Record<string, unknown>} />
                    ) : (
                      <span className="break-all">{String(v)}</span>
                    )}
                  </span>
                </div>
              ))}
          </div>
        </section>
      )}

      <YAMLPreview data={data} />
    </div>
  );
}

function ExecResultLabel({ result }: { result?: string }) {
  const cls =
    result === "ok" ? "text-[var(--vigil-ok)]"
    : result === "rolled_back" ? "text-[var(--vigil-warn)]"
    : result === "blocked" ? "text-[var(--vigil-warn)]"
    : "text-[var(--vigil-error)]";
  const label =
    result === "ok" ? "成功"
    : result === "rolled_back" ? "已回滚"
    : result === "blocked" ? "被拦截"
    : result === "failed" ? "失败"
    : String(result ?? "未知");
  return <span className={`font-semibold ${cls}`}>{label}</span>;
}

function ExecSteps({ steps }: { steps?: RunbookStepResult[] }) {
  if (!steps || steps.length === 0) return null;
  return (
    <ol className="mt-2 space-y-1.5">
      {steps.map((st, i) => {
        const key = String(st.id ?? `step-${i}`);
        const ok = st.ok || st.status === "ok";
        const fail = st.status === "failed" || st.status === "blocked" || st.status === "error";
        const desc = (st.commands ?? [])
          .map((c) => c.desc ?? c.command ?? "")
          .filter(Boolean)
          .join("；");
        return (
          <li key={key} className="flex items-start gap-2 text-xs">
            <span
              className={`mt-0.5 shrink-0 rounded px-1.5 py-px font-mono text-[10px] ${
                ok ? "bg-[var(--vigil-muted-bg)] text-[var(--vigil-ok)]"
                : fail ? "bg-[var(--vigil-muted-bg)] text-[var(--vigil-error)]"
                : "bg-[var(--vigil-muted-bg)] text-[var(--vigil-muted)]"
              }`}
            >
              {String(st.status ?? "?")}
            </span>
            <div className="min-w-0 flex-1">
              <div className="font-medium text-[var(--vigil-text)] opacity-90">
                {key}
                {st.action ? <span className="ml-1.5 text-[var(--vigil-muted)]">· {String(st.action)}</span> : null}
              </div>
              {desc ? <div className="truncate text-[var(--vigil-muted)]">{desc}</div> : null}
              {st.error ? <div className="text-[var(--vigil-error)]">{String(st.error).slice(0, 400)}</div> : null}
              {st.steps && st.steps.length > 0 ? (
                <div className="mt-1 border-l border-[var(--vigil-border)] pl-2">
                  <div className="text-[10px] text-[var(--vigil-muted)]">回滚场景</div>
                  <ExecSteps steps={st.steps} />
                </div>
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

// ── 批八十：runbook 执行实时进度面板（SSE 事件流渲染）────────────────────

interface ProgressRow {
  key: string;
  stepId: string;
  title: string;
  action: string;
  target: string;
  phase: string;
  status: string;
  detail?: string;
  ts?: string;
}

function buildProgressRows(events: RunbookProgressEvent[]): {
  rows: ProgressRow[];
  banners: RunbookProgressEvent[];
} {
  const rows: ProgressRow[] = [];
  const banners: RunbookProgressEvent[] = [];
  const index = new Map<string, number>();
  for (const ev of events) {
    if (ev.type === "step_start") {
      const key = `${ev.phase ?? "runbook"}:${ev.step_id ?? ""}`;
      rows.push({
        key,
        stepId: ev.step_id ?? "",
        title: ev.title ?? ev.step_id ?? "",
        action: ev.action ?? "",
        target: ev.target ?? "",
        phase: ev.phase ?? "runbook",
        status: "running",
        ts: ev.ts,
      });
      index.set(key, rows.length - 1);
    } else if (ev.type === "step_done" || ev.type === "step_failed") {
      const key = `${ev.phase ?? "runbook"}:${ev.step_id ?? ""}`;
      const i = index.get(key);
      if (i !== undefined) {
        rows[i] = {
          ...rows[i],
          status: ev.type === "step_done" ? "ok" : "failed",
          detail: ev.detail,
          ts: ev.ts,
        };
      }
    } else {
      banners.push(ev);
    }
  }
  return { rows, banners };
}

function ProgressStatusBadge({ status }: { status?: string }) {
  if (status === "ok") {
    return <span className="shrink-0 rounded bg-[var(--vigil-muted-bg)] px-1.5 py-px font-mono text-[10px] text-[var(--vigil-ok)]">成功</span>;
  }
  if (status === "failed" || status === "error") {
    return <span className="shrink-0 rounded bg-[var(--vigil-muted-bg)] px-1.5 py-px font-mono text-[10px] text-[var(--vigil-error)]">失败</span>;
  }
  if (status === "blocked") {
    return <span className="shrink-0 rounded bg-[var(--vigil-muted-bg)] px-1.5 py-px font-mono text-[10px] text-[var(--vigil-warn)]">被拦截</span>;
  }
  if (status === "rolled_back") {
    return <span className="shrink-0 rounded bg-[var(--vigil-muted-bg)] px-1.5 py-px font-mono text-[10px] text-[var(--vigil-warn)]">已回滚</span>;
  }
  return (
    <span className="inline-flex shrink-0 items-center gap-1 rounded bg-[var(--vigil-muted-bg)] px-1.5 py-px font-mono text-[10px] text-[var(--vigil-muted)]">
      <Loader2 className="size-2.5 animate-spin" /> 运行中
    </span>
  );
}

function RunbookProgressPanel({
  events,
  terminal,
  loading,
}: {
  events: RunbookProgressEvent[];
  terminal?: RunbookProgressEvent | null;
  loading?: boolean;
}) {
  const { rows, banners } = buildProgressRows(events);
  return (
    <div data-testid="runbook-progress" className="mt-2 space-y-1.5">
      {rows.length === 0 && loading ? (
        <div className="flex items-center gap-2 text-xs text-[var(--vigil-muted)]">
          <Loader2 className="size-3 animate-spin" /> 等待执行启动…
        </div>
      ) : null}
      {rows.map((r) => (
        <div key={r.key} className="flex items-start gap-2 text-xs">
          <ProgressStatusBadge status={r.status} />
          <div className="min-w-0 flex-1">
            <div className="font-medium text-[var(--vigil-text)] opacity-90">
              {r.phase === "rollback" ? "↩ " : ""}
              {r.title}
              <span className="ml-1.5 text-[10px] text-[var(--vigil-muted)]">
                id: {r.stepId}
                {r.action ? ` · ${r.action}` : ""}
                {r.target ? ` · target: ${r.target}` : ""}
              </span>
            </div>
            {r.detail ? (
              <div className="truncate text-[var(--vigil-muted)]" title={r.detail}>{r.detail}</div>
            ) : null}
          </div>
        </div>
      ))}
      {banners.map((b, i) => (
        <div
          key={`banner-${i}`}
          className={`rounded border px-2 py-1 text-xs ${
            b.type === "rollback_start"
              ? "border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400"
              : b.type === "rollback_done"
                ? b.status === "ok"
                  ? "border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400"
                  : "border-red-500/40 bg-red-500/10 text-red-600 dark:text-red-400"
                : "border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)]"
          }`}
        >
          {b.type === "rollback_start" ? "↩ 触发回滚" : b.type === "rollback_done" ? "↩ 回滚完成" : ""}
          {b.detail ? <span className="ml-1 text-[var(--vigil-muted)]">{b.detail}</span> : null}
        </div>
      ))}
      {terminal ? (
        <div className="mt-1 flex items-center gap-2 border-t border-[var(--vigil-border)] pt-1.5 text-xs">
          <ProgressStatusBadge status={terminal.status} />
          <span className="font-semibold">执行完成</span>
          {terminal.duration_s !== undefined ? (
            <span className="text-[var(--vigil-muted)]">{(terminal.duration_s ?? 0).toFixed(1)}s</span>
          ) : null}
          {terminal.error ? (
            <span className="min-w-0 flex-1 truncate text-[var(--vigil-error)]" title={terminal.error}>
              {terminal.error}
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function ExecConfirmModal({
  name,
  running,
  onCancel,
  onConfirm,
}: {
  name: string;
  running: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-md rounded-lg border border-[var(--vigil-border)] bg-[var(--vigil-card)] p-5 shadow-lg">
        <div className="flex items-center gap-2">
          <Play className="size-4 text-[var(--vigil-primary)]" />
          <h2 className="text-sm font-semibold">执行 runbook</h2>
          <button className="ml-auto rounded p-1 hover:bg-[var(--vigil-muted-bg)]" onClick={onCancel} aria-label="关闭" disabled={running}>
            <X className="size-4" />
          </button>
        </div>
        <p className="mt-3 text-sm text-[var(--vigil-text)] opacity-80">
          确认执行 <code className="font-mono">{name}</code>？步骤按操作矩阵逐次裁决
          （execute 直跑；approve / 强制人工 → 右下角审批卡，需人工确认）。
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" className="vigil-btn border border-[var(--vigil-border)] px-3 py-1 text-xs" onClick={onCancel} disabled={running}>
            取消
          </button>
          <button
            type="button"
            className="vigil-btn px-3 py-1 text-xs"
            onClick={onConfirm}
            disabled={running}
          >
            {running ? <Loader2 className="mr-1 inline size-3.5 animate-spin" /> : null}
            确认执行
          </button>
        </div>
      </div>
    </div>
  );
}

export default function RunbooksPage() {
  const [searchParams] = useSearchParams();
  const urlName = searchParams.get("name");
  const [list, setList] = useState<RunbookSummary[] | null>(null);
  const [selected, setSelected] = useState<string | null>(urlName);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [execModal, setExecModal] = useState(false);
  const [execRunning, setExecRunning] = useState(false);
  const [execResult, setExecResult] = useState<RunbookExecution | null>(null);
  const [execError, setExecError] = useState<string | null>(null);
  const [history, setHistory] = useState<RunbookExecution[]>([]);
  // 批八十：实时进度（SSE 事件流）+ 历史"运行中"行。
  const [execEvents, setExecEvents] = useState<RunbookProgressEvent[]>([]);
  const [execDone, setExecDone] = useState<RunbookProgressEvent | null>(null);
  const [running, setRunning] = useState<RunbookRunningExec[]>([]);
  // 批八十一：执行级并发锁快照（同名 runbook / 同目标禁止并发下发）。
  const [locks, setLocks] = useState<RunbookLock[]>([]);
  const [coverage, setCoverage] = useState<RunbookCoverageResponse["data"] | null>(null);
  const [runningEvents, setRunningEvents] = useState<Record<string, RunbookProgressEvent[]>>({});
  const [expandedRunning, setExpandedRunning] = useState<Set<string>>(new Set());
  const streamAbortRef = useRef<AbortController | null>(null);
  const runningStreamsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    let alive = true;
    api
      .getRunbookExecutions(20)
      .then((resp) => {
        if (alive && resp.ok && resp.data) {
          setHistory(resp.data.executions);
          setRunning(resp.data.running ?? []);
          setLocks(resp.data.locks ?? []);
        }
      })
      .catch(() => {});
    api
      .getRunbookCoverage()
      .then((resp) => {
        if (alive && resp.ok && resp.data) setCoverage(resp.data);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  // 批八十一：无实时流（定时/后台/LLM）的锁定中执行 → 仅显示锁定徽标，不可展开。
  const lockOnly = locks.filter((l) => !running.some((r) => r.exec_id === l.exec_id));

  // 批八十：卸载时中止进行中的进度流（服务端执行不受影响）。
  useEffect(() => {
    return () => {
      streamAbortRef.current?.abort();
    };
  }, []);

  const refreshHistory = () => {
    api
      .getRunbookExecutions(20)
      .then((resp) => {
        if (resp.ok && resp.data) {
          setHistory(resp.data.executions);
          setRunning(resp.data.running ?? []);
          setLocks(resp.data.locks ?? []);
        }
      })
      .catch(() => {});
  };

  const runSelected = () => {
    if (!selected || execRunning) return;
    setExecRunning(true);
    setExecError(null);
    setExecResult(null);
    setExecEvents([]);
    setExecDone(null);
    setExecModal(false);
    api
      .runRunbook(selected)
      .then((resp) => {
        if (!resp.ok || !resp.data?.exec_id) {
          setExecError(resp.error ?? "执行启动失败");
          setExecRunning(false);
          return;
        }
        const execId = resp.data.exec_id;
        const ctrl = new AbortController();
        streamAbortRef.current = ctrl;
        void api
          .runbookProgressStream(
            execId,
            (ev) => {
              if (ev.type === "runbook_done") {
                setExecDone(ev);
                setExecRunning(false);
                setExecResult({
                  runbook: selected,
                  result: ev.status,
                  error: ev.error,
                  duration_s: ev.duration_s,
                  ts: ev.ts,
                });
                refreshHistory();
              } else {
                setExecEvents((prev) => [...prev, ev]);
              }
            },
            ctrl.signal,
          )
          .catch((e: unknown) => {
            setExecError(e instanceof Error ? e.message : String(e));
            setExecRunning(false);
          });
      })
      .catch((e: unknown) => {
        setExecError(e instanceof Error ? e.message : String(e));
      });
  };

  // 批八十：历史"运行中"行展开 → 打开该执行的实时进度流。
  const startRunningStream = (execId: string) => {
    if (runningStreamsRef.current.has(execId)) return;
    runningStreamsRef.current.add(execId);
    const ctrl = new AbortController();
    streamAbortRef.current = ctrl;
    void api
      .runbookProgressStream(
        execId,
        (ev) => {
          setRunningEvents((prev) => ({
            ...prev,
            [execId]: [...(prev[execId] ?? []), ev],
          }));
          if (ev.type === "runbook_done") {
            setExpandedRunning((prev) => {
              const n = new Set(prev);
              n.delete(execId);
              return n;
            });
            refreshHistory();
          }
        },
        ctrl.signal,
      )
      .catch(() => {});
  };

  const toggleRunningRow = (execId: string) => {
    setExpandedRunning((prev) => {
      const n = new Set(prev);
      if (n.has(execId)) n.delete(execId);
      else n.add(execId);
      return n;
    });
    startRunningStream(execId);
  };

  useEffect(() => {
    let alive = true;
    api
      .getRunbooks()
      .then((resp) => {
        if (!alive) return;
        if (resp.ok && resp.data) {
          setList(resp.data.runbooks);
          if (!selected && resp.data.runbooks.length > 0) {
            setSelected(resp.data.runbooks[0].name);
          }
        }
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let alive = true;
    setDetailLoading(true);
    setDetailError(null);
    api
      .getRunbook(selected)
      .then((resp: RunbookDetailResponse) => {
        if (!alive) return;
        if (resp.ok && resp.data) setDetail(resp.data);
        else {
          setDetail(null);
          setDetailError(resp.error ?? "详情加载失败");
        }
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setDetail(null);
        setDetailError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (alive) setDetailLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [selected]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <BookOpen className="size-5 text-[var(--vigil-muted)]" />
          <h1 className="text-lg font-semibold">Runbooks</h1>
        </div>
        <span className="ml-auto text-xs text-[var(--vigil-muted)]">
          只读 · 内容已脱敏（路径/密钥/URL userinfo 过滤）
        </span>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[minmax(320px,420px)_1fr]">
        {/* 紧凑表格 */}
        <div className="scroll-thin min-h-0 overflow-y-auto">
          {list && list.length > 0 ? (
            <div className="vigil-card">
              <table className="vigil-table">
                <thead>
                  <tr>
                    <th>名称</th>
                    <th>env</th>
                    <th>kind</th>
                    <th className="text-right">步骤</th>
                    <th className="text-right">更新</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((rb) => (
                    <tr
                      key={rb.name}
                      onClick={() => setSelected(rb.name)}
                      className={cn(selected === rb.name && "bg-[var(--vigil-muted-bg)]")}
                    >
                      <td>
                        <div className="font-medium">{rb.title}</div>
                        <div className="font-mono text-[10px] text-[var(--vigil-muted)]">{rb.name}</div>
                      </td>
                      <td><EnvTag env={rb.env} /></td>
                      <td><KindTag kind={rb.kind} checklist={rb.checklist} /></td>
                      <td className="text-right text-xs text-[var(--vigil-muted)]">{rb.step_count}</td>
                      <td className="text-right">
                        <span className="inline-flex items-center gap-1 text-[10px] text-[var(--vigil-muted)]">
                          <Clock className="size-3" />
                          {rb.updated_at ? String(rb.updated_at).slice(0, 10) : "-"}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState
              title="无 runbook 数据"
              hint="先运行 vigil topo-discover 或创建 runbooks/*.yaml"
            />
          )}
        </div>

        {/* 详情 */}
        <div className="scroll-thin min-h-0 overflow-y-auto">
          {!selected ? (
            <div className="vigil-card border-dashed flex items-center justify-center gap-2 p-10 text-sm text-[var(--vigil-muted)]">
              <FileText className="size-4" /> 选择左侧 runbook 查看详情
            </div>
          ) : detailLoading ? (
            <div className="flex items-center justify-center gap-2 py-16 text-sm text-[var(--vigil-muted)]">
              加载详情…
            </div>
          ) : detailError ? (
            <div className="vigil-card border-dashed p-10 text-center text-sm text-[var(--vigil-muted)]">
              {detailError}
            </div>
          ) : detail ? (
            <div className="space-y-4">
              <div className="vigil-card p-5">
                <div className="mb-3 flex flex-wrap items-center gap-2">
                  <h2 className="text-base font-semibold">{String(detail.title ?? detail.name ?? "")}</h2>
                  <EnvTag env={String(detail.env ?? "")} />
                  <KindTag kind={String(detail.kind ?? "")} checklist={Boolean(detail.checklist)} />
                  {String(detail.version) === "2" ? (
                    <button
                      type="button"
                      className="vigil-btn ml-auto border border-[var(--vigil-primary)]/40 px-3 py-1 text-xs"
                      onClick={() => setExecModal(true)}
                      disabled={execRunning}
                    >
                      {execRunning ? <Loader2 className="mr-1 inline size-3.5 animate-spin" /> : <Play className="mr-1 inline size-3.5" />}
                      {execRunning ? "执行中…" : "执行"}
                    </button>
                  ) : null}
                </div>
                <RunbookDetail data={detail} />
              </div>

              {(execRunning || execDone || execEvents.length > 0) ? (
                <div className="vigil-card p-5">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-xs font-semibold">执行进度</h3>
                    {execRunning ? (
                      <span className="text-[10px] text-[var(--vigil-muted)]">实时流 · 关键节点自动播报</span>
                    ) : null}
                  </div>
                  <RunbookProgressPanel events={execEvents} terminal={execDone} loading={execRunning} />
                </div>
              ) : null}

              {execResult ? (
                <div className="vigil-card p-5">
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <h3 className="text-xs font-semibold">执行结果</h3>
                    <ExecResultLabel result={String(execResult.result ?? "")} />
                    <span className="text-[10px] text-[var(--vigil-muted)]">
                      {String(execResult.ts ?? "")} · {String(execResult.env ?? "")} ·{" "}
                      {(execResult.duration_s ?? 0).toFixed(1)}s
                    </span>
                  </div>
                  {execResult.error ? (
                    <p className="mt-2 break-words text-xs text-[var(--vigil-error)]">{execResult.error}</p>
                  ) : null}
                  <ExecSteps steps={execResult.steps} />
                </div>
              ) : null}

              {execError ? (
                <div className="vigil-card border-dashed p-5 text-sm text-[var(--vigil-error)]">
                  执行失败：{execError}
                </div>
              ) : null}

              <div className="vigil-card p-5">
                <div className="flex items-center gap-2">
                  <h3 className="text-xs font-semibold">执行历史（最近 {history.length} 次）</h3>
                  <button
                    type="button"
                    className="ml-auto rounded border border-[var(--vigil-border)] px-2 py-0.5 text-[10px] hover:border-[var(--vigil-primary)]"
                    onClick={refreshHistory}
                  >
                    刷新
                  </button>
                </div>
                {history.length > 0 || running.length > 0 || lockOnly.length > 0 ? (
                  <table className="vigil-table mt-2">
                    <thead>
                      <tr>
                        <th>runbook</th>
                        <th>时间</th>
                        <th>来源</th>
                        <th>结果</th>
                      </tr>
                    </thead>
                    <tbody>
                      {running.map((r) => (
                        <Fragment key={r.exec_id}>
                          <tr
                            className="cursor-pointer hover:bg-[var(--vigil-muted-bg)]"
                            onClick={() => toggleRunningRow(r.exec_id)}
                            title="点击展开实时步骤"
                          >
                            <td className="font-mono text-xs">
                              {String(r.runbook ?? "")}
                              {expandedRunning.has(r.exec_id) ? (
                                <ChevronDown className="ml-1 inline size-3 text-[var(--vigil-muted)]" />
                              ) : (
                                <ChevronRight className="ml-1 inline size-3 text-[var(--vigil-muted)]" />
                              )}
                            </td>
                            <td className="text-xs text-[var(--vigil-muted)]">{String(r.started_at ?? "")}</td>
                            <td className="text-xs text-[var(--vigil-muted)]">web</td>
                            <td className="text-xs">
                              <span className="inline-flex items-center gap-1 font-semibold text-[var(--vigil-muted)]">
                                <Loader2 className="size-3 animate-spin" /> 运行中
                                <span className="ml-1 inline-flex items-center gap-1 rounded border border-[var(--vigil-border)] px-1 py-px text-[10px] font-medium text-[var(--vigil-muted)]">
                                  <Lock className="size-2.5 text-[var(--vigil-warning)]" /> 锁定中
                                </span>
                              </span>
                            </td>
                          </tr>
                          {expandedRunning.has(r.exec_id) ? (
                            <tr>
                              <td colSpan={4} className="bg-[var(--vigil-muted-bg)]/40">
                                <RunbookProgressPanel
                                  events={runningEvents[r.exec_id] ?? []}
                                  loading={!runningEvents[r.exec_id] || runningEvents[r.exec_id].length === 0}
                                />
                              </td>
                            </tr>
                          ) : null}
                        </Fragment>
                      ))}
                      {lockOnly.map((lk) => (
                        <tr key={`lock-${lk.exec_id}`} className="opacity-90">
                          <td className="font-mono text-xs">{String(lk.runbook ?? "")}</td>
                          <td className="text-xs text-[var(--vigil-muted)]">{String(lk.started_at ?? "")}</td>
                          <td className="text-xs text-[var(--vigil-muted)]">定时/后台</td>
                          <td className="text-xs">
                            <span className="inline-flex items-center gap-1 font-semibold text-[var(--vigil-muted)]">
                              <Lock className="size-3 text-[var(--vigil-warning)]" /> 锁定中
                              <span className="font-normal text-[10px]">（无实时流）</span>
                            </span>
                          </td>
                        </tr>
                      ))}
                      {history.map((h, i) => (
                        <tr key={`${String(h.runbook ?? "")}-${String(h.ts ?? "")}-${i}`}>
                          <td className="font-mono text-xs">{String(h.runbook ?? "")}</td>
                          <td className="text-xs text-[var(--vigil-muted)]">{String(h.ts ?? "")}</td>
                          <td className="text-xs text-[var(--vigil-muted)]">{String(h.source ?? "")}</td>
                          <td className="text-xs">
                            <ExecResultLabel result={String(h.result ?? "")} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <p className="mt-2 text-xs text-[var(--vigil-muted)]">
                    暂无执行记录（交互 / 定时执行都会落到 runtime/runbook_executions.jsonl）
                  </p>
                )}
              </div>
            </div>
          ) : null}
        </div>
      </div>

      {/* 批八十一：Runbook 覆盖率（审计动作使用率 × runbook 覆盖，只读统计） */}
      <div className="vigil-card p-5">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold">Runbook 覆盖率</h3>
          <span className="text-[10px] text-[var(--vigil-muted)]">
            {coverage ? `近 ${coverage.usage.window_days} 天审计动作 × runbook 覆盖 · 只读统计` : "加载中…"}
          </span>
        </div>
        {coverage ? (
          coverage.usage.total_unique === 0 ? (
            <p className="mt-3 text-xs text-[var(--vigil-muted)]">
              暂无审计数据——terminal / runbook 执行过命令后，这里统计动作使用频率与 runbook
              覆盖缺口（classifier 识别不出的动作不计入）。
            </p>
          ) : (
            <>
              <div className="mt-3 flex flex-wrap gap-4 text-xs text-[var(--vigil-muted)]">
                <span>动作 {coverage.usage.total_unique}</span>
                <span>已覆盖 {coverage.usage.covered_unique}</span>
                <span>覆盖率 {coverage.usage.coverage_pct}%</span>
                <span>扫描审计事件 {coverage.usage.audit_events_scanned}</span>
              </div>
              <table className="vigil-table mt-3">
                <thead>
                  <tr>
                    <th>动作</th>
                    <th className="text-right">使用次数</th>
                    <th>覆盖</th>
                    <th>runbooks</th>
                  </tr>
                </thead>
                <tbody>
                  {coverage.usage.actions.map((a) => (
                    <tr key={a.action}>
                      <td className="font-mono text-xs">{a.action}</td>
                      <td className="text-right text-xs">{a.use_count}</td>
                      <td className="text-xs">
                        {a.covered ? (
                          <span className="font-medium text-emerald-600 dark:text-emerald-400">已覆盖</span>
                        ) : (
                          <span className="font-medium text-amber-600 dark:text-amber-400">未覆盖</span>
                        )}
                      </td>
                      <td className="text-xs text-[var(--vigil-muted)]">
                        {a.runbooks.length > 0 ? a.runbooks.join(", ") : "-"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {coverage.usage.gaps.length > 0 ? (
                <div className="mt-3 rounded border border-amber-500/30 bg-amber-500/5 p-3 text-xs">
                  <span className="font-semibold">高频未覆盖，建议沉淀 runbook：</span>
                  {coverage.usage.gaps.map((g) => `${g.action}（${g.use_count} 次）`).join("、")}
                </div>
              ) : null}
            </>
          )
        ) : (
          <p className="mt-3 text-xs text-[var(--vigil-muted)]">覆盖率加载中…</p>
        )}
      </div>

      {execModal && (
        <ExecConfirmModal
          name={selected ?? ""}
          running={execRunning}
          onCancel={() => setExecModal(false)}
          onConfirm={runSelected}
        />
      )}
    </div>
  );
}
