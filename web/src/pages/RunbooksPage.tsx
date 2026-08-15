import { useEffect, useState } from "react";
import { useSearchParams } from "react-router";
import {
  BookOpen,
  ChevronDown,
  ChevronRight,
  Clock,
  FileText,
  ListChecks,
  RefreshCw,
  ShieldAlert,
  Terminal,
  TriangleAlert,
} from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { api } from "@/lib/api";
import type { RunbookDetailResponse, RunbookListResponse, RunbookSummary } from "@/lib/api";
import { OpsKeyValueTree } from "@/components/OpsKeyValueTree";
import { EmptyState } from "@/components/ops/EmptyState";
import { cn } from "@/lib/utils";
import { yamlPreview } from "@/lib/ops";

function EnvTag({ env }: { env?: string }) {
  if (!env) return null;
  const color =
    env === "prod"
      ? "bg-red-600"
      : env === "test"
        ? "bg-amber-600"
        : env === "uat"
          ? "bg-orange-600"
          : "bg-slate-500";
  return (
    <span className={cn("rounded px-1.5 py-px text-[10px] text-white", color)}>{env}</span>
  );
}

function KindTag({ kind, checklist }: { kind?: string; checklist?: boolean }) {
  const label =
    kind === "deploy" ? "部署" : kind === "incident" ? "事故" : kind ?? "runbook";
  return (
    <span className="rounded border border-border px-1.5 py-px text-[10px] text-muted-foreground">
      {label}
      {checklist ? " · checklist" : ""}
    </span>
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
        className="inline-flex items-center gap-1 rounded border border-border bg-muted/40 px-2 py-0.5 text-xs text-muted-foreground hover:bg-muted"
        aria-expanded={open}
      >
        {open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
        YAML 预览（已脱敏）
      </button>
      {open && (
        <pre className="mt-2 max-h-80 overflow-auto rounded border border-border bg-[#0b0f14] p-3 font-mono text-[11px] leading-relaxed text-slate-300">
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
  ]);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
        <span>
          name: <span className="text-foreground/80">{String(data.name ?? "")}</span>
        </span>
        <span>
          env: <span className="text-foreground/80">{String(data.env ?? "-")}</span>
        </span>
        <span>
          kind: <span className="text-foreground/80">{String(data.kind ?? "-")}</span>
        </span>
        <span>
          更新: <span className="text-foreground/80">{String(data.updated_at ?? "-")}</span>
        </span>
      </div>

      {data.summary ? <p className="text-sm text-foreground/80">{String(data.summary)}</p> : null}

      {triggers.length > 0 && (
        <section>
          <h3 className="mb-2 flex items-center gap-2 text-xs font-semibold">
            <TriangleAlert className="size-3.5 text-muted-foreground" /> 触发条件
          </h3>
          <div className="flex flex-wrap gap-1.5">
            {triggers.map((t, i) => (
              <span key={i} className="rounded border border-border bg-muted/40 px-2 py-0.5 text-xs">
                {String(t)}
              </span>
            ))}
          </div>
        </section>
      )}

      {steps.length > 0 && (
        <section>
          <h3 className="mb-2 flex items-center gap-2 text-xs font-semibold">
            <ListChecks className="size-3.5 text-muted-foreground" /> 步骤（{steps.length}）
          </h3>
          <ol className="space-y-2">
            {steps.map((step, i) => (
              <li key={(step.id as string) ?? i} className="rounded border border-border bg-card p-3">
                <div className="mb-1.5 flex flex-wrap items-center gap-2 text-sm">
                  <span className="flex size-5 items-center justify-center rounded-full bg-muted text-xs">
                    {i + 1}
                  </span>
                  <span className="font-medium">{String(step.title ?? step.id ?? i)}</span>
                  <span className="text-xs text-muted-foreground">id: {String(step.id ?? "")}</span>
                </div>
                {Array.isArray(step.commands) && step.commands.length > 0 && (
                  <div className="space-y-1">
                    {step.commands.map((cmd, j) => (
                      <pre
                        key={j}
                        className="overflow-x-auto rounded bg-muted/50 px-2.5 py-1.5 font-mono text-[11px] text-foreground/85"
                      >
                        {String(cmd)}
                      </pre>
                    ))}
                  </div>
                )}
                {(step.verify || step.expect) ? (
                  <div className="mt-1.5 space-y-0.5 text-xs text-muted-foreground">
                    {step.verify ? (
                      <div>
                        verify:{" "}
                        <code className="font-mono text-foreground/75">{String(step.verify)}</code>
                      </div>
                    ) : null}
                    {step.expect ? (
                      <div>
                        expect:{" "}
                        <code className="font-mono text-foreground/75">{String(step.expect)}</code>
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </li>
            ))}
          </ol>
        </section>
      )}

      {rollback.length > 0 && (
        <section>
          <h3 className="mb-2 flex items-center gap-2 text-xs font-semibold">
            <Terminal className="size-3.5 text-muted-foreground" /> 回滚预案
          </h3>
          <div className="space-y-1.5">
            {rollback.map((rb, i) => (
              <div key={i} className="rounded border border-border bg-card p-3">
                {rb.title ? (
                  <div className="mb-1 text-sm">{String(rb.title)}</div>
                ) : null}
                {Array.isArray(rb.commands) &&
                  rb.commands.map((cmd, j) => (
                    <pre
                      key={j}
                      className="overflow-x-auto rounded bg-muted/50 px-2.5 py-1.5 font-mono text-[11px] text-foreground/85"
                    >
                      {String(cmd)}
                    </pre>
                  ))}
                {rb.note ? (
                  <div className="mt-1 text-xs text-muted-foreground">{String(rb.note)}</div>
                ) : null}
              </div>
            ))}
          </div>
        </section>
      )}

      {Object.keys(data).some((k) => !known.has(k)) && (
        <section>
          <h3 className="mb-2 text-xs font-semibold">其他字段</h3>
          <div className="rounded border border-border bg-card p-3">
            {Object.entries(data)
              .filter(([k]) => !known.has(k))
              .map(([k, v]) => (
                <div
                  key={k}
                  className="flex gap-2 border-b border-dotted border-border/60 py-1.5 text-sm last:border-b-0"
                >
                  <span className="min-w-32 shrink-0 text-muted-foreground">{k}</span>
                  <span className="min-w-0 flex-1">
                    {typeof v === "object" && v !== null ? (
                      <OpsKeyValueTree data={v as Record<string, unknown>} />
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
  const [listError, setListError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(urlName);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const loadList = () => {
    setLoading(true);
    setListError(null);
    api
      .getRunbooks()
      .then((resp: RunbookListResponse) => {
        if (resp.ok && resp.data) {
          setList(resp.data.runbooks);
          if (!selected && resp.data.runbooks.length > 0) {
            setSelected(resp.data.runbooks[0].name);
          }
        } else {
          setList(null);
          setListError(resp.error ?? "runbook 列表加载失败");
        }
      })
      .catch((e: unknown) => {
        setList(null);
        setListError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => setLoading(false));
  };

  useEffect(loadList, []);

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
        if (resp.ok && resp.data) {
          setDetail(resp.data);
        } else {
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
    <div className="mx-auto w-full max-w-7xl p-4 lg:p-6">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <BookOpen className="size-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">Runbook 剧本</h1>
        </div>
        <Button ghost size="sm" onClick={loadList} aria-label="刷新">
          <RefreshCw className="size-4" />
        </Button>
        <span className="ml-auto text-xs text-muted-foreground">
          只读 · 内容已脱敏（路径/密钥/URL userinfo 过滤）
        </span>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(320px,420px)_1fr]">
        {/* Compact table */}
        <div>
          {loading ? (
            <div className="flex items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
              <Spinner />
              <span>加载中…</span>
            </div>
          ) : listError ? (
            <Card className="border-dashed">
              <CardContent className="flex flex-col items-center gap-3 p-8 text-center">
                <ShieldAlert className="size-7 text-muted-foreground" />
                <div className="text-sm text-muted-foreground">{listError}</div>
                <Button size="sm" onClick={loadList}>
                  重试
                </Button>
              </CardContent>
            </Card>
          ) : list && list.length > 0 ? (
            <Card className="border-border bg-card">
              <CardContent className="p-0">
                <table className="w-full border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-[11px] text-muted-foreground">
                      <th className="px-3 py-2 font-medium">名称</th>
                      <th className="px-2 py-2 font-medium">env</th>
                      <th className="px-2 py-2 font-medium">kind</th>
                      <th className="px-2 py-2 text-right font-medium">步骤</th>
                      <th className="px-3 py-2 text-right font-medium">更新</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((rb) => (
                      <tr
                        key={rb.name}
                        onClick={() => setSelected(rb.name)}
                        className={cn(
                          "cursor-pointer border-b border-border/60 last:border-b-0 hover:bg-muted/40",
                          selected === rb.name && "bg-muted/60",
                        )}
                      >
                        <td className="px-3 py-2">
                          <div className="font-medium">{rb.title}</div>
                          <div className="font-mono text-[10px] text-muted-foreground">
                            {rb.name}
                          </div>
                        </td>
                        <td className="px-2 py-2">
                          <EnvTag env={rb.env} />
                        </td>
                        <td className="px-2 py-2">
                          <KindTag kind={rb.kind} checklist={rb.checklist} />
                        </td>
                        <td className="px-2 py-2 text-right text-xs text-muted-foreground">
                          {rb.step_count}
                        </td>
                        <td className="px-3 py-2 text-right">
                          <span className="inline-flex items-center gap-1 text-[10px] text-muted-foreground">
                            <Clock className="size-3" />
                            {rb.updated_at ? String(rb.updated_at).slice(0, 10) : "-"}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          ) : (
            <EmptyState
              title="无 runbook 数据"
              hint="先运行 vigil topo-discover 或创建 runbooks/*.yaml"
            />
          )}
        </div>

        {/* Detail */}
        <div>
          {!selected ? (
            <Card className="border-dashed">
              <CardContent className="flex items-center justify-center gap-2 p-10 text-sm text-muted-foreground">
                <FileText className="size-4" /> 选择左侧 runbook 查看详情
              </CardContent>
            </Card>
          ) : detailLoading ? (
            <div className="flex items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
              <Spinner />
              <span>加载详情…</span>
            </div>
          ) : detailError ? (
            <Card className="border-dashed">
              <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
                <ShieldAlert className="size-7 text-muted-foreground" />
                <div className="text-sm text-muted-foreground">{detailError}</div>
              </CardContent>
            </Card>
          ) : detail ? (
            <Card className="border-border bg-card">
              <CardContent className="p-5">
                <div className="mb-3 flex flex-wrap items-center gap-2">
                  <h2 className="text-base font-semibold">
                    {String(detail.title ?? detail.name ?? "")}
                  </h2>
                  <EnvTag env={String(detail.env ?? "")} />
                  <KindTag kind={String(detail.kind ?? "")} checklist={Boolean(detail.checklist)} />
                </div>
                <RunbookDetail data={detail} />
              </CardContent>
            </Card>
          ) : null}
        </div>
      </div>
    </div>
  );
}
