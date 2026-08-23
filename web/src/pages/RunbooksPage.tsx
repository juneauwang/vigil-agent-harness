import { useEffect, useState } from "react";
import { useSearchParams } from "react-router";
import {
  BookOpen,
  ChevronDown,
  ChevronRight,
  Clock,
  FileText,
  ListChecks,
  Terminal,
  TriangleAlert,
} from "lucide-react";
import { api } from "@/lib/api";
import type { RunbookDetailResponse, RunbookSummary } from "@/lib/api";
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

export default function RunbooksPage() {
  const [searchParams] = useSearchParams();
  const urlName = searchParams.get("name");
  const [list, setList] = useState<RunbookSummary[] | null>(null);
  const [selected, setSelected] = useState<string | null>(urlName);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

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
            <div className="vigil-card p-5">
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <h2 className="text-base font-semibold">{String(detail.title ?? detail.name ?? "")}</h2>
                <EnvTag env={String(detail.env ?? "")} />
                <KindTag kind={String(detail.kind ?? "")} checklist={Boolean(detail.checklist)} />
              </div>
              <RunbookDetail data={detail} />
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
