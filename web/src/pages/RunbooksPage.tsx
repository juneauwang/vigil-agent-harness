import { useEffect, useState } from "react";
import {
  BookOpen,
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
import { Badge } from "@nous-research/ui/ui/components/badge";
import { api } from "@/lib/api";
import type { RunbookDetailResponse, RunbookListResponse, RunbookSummary } from "@/lib/api";
import { OpsKeyValueTree } from "@/components/OpsKeyValueTree";
import { cn } from "@/lib/utils";

function KindBadge({ kind, checklist }: { kind?: string; checklist?: boolean }) {
  return (
    <Badge tone="default" className="text-xs">
      {kind === "deploy" ? "部署" : kind === "incident" ? "事故" : kind ?? "runbook"}
      {checklist ? " · checklist" : ""}
    </Badge>
  );
}

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
    <Badge tone="default" className={cn("border-transparent text-white", color)}>
      {env}
    </Badge>
  );
}

function DetailValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="text-muted-foreground/60">-</span>;
  if (typeof value === "object") {
    return <OpsKeyValueTree data={value as Record<string, unknown>} className="w-full" />;
  }
  return <span className="break-all">{String(value)}</span>;
}

/** Render a runbook's known fields as a definition list; unknown fields
 *  fall back to the generic tree so nothing is silently dropped. */
function RunbookDetailBody({ data }: { data: Record<string, unknown> }) {
  const steps = (data.steps as Array<Record<string, unknown>>) ?? [];
  const rollback = (data.rollback as Array<Record<string, unknown>>) ?? [];
  const triggers = (data.triggers as unknown[]) ?? [];
  const known = new Set([
    "name", "title", "version", "env", "kind", "checklist", "triggers",
    "summary", "steps", "rollback", "updated_at", "note",
  ]);

  return (
    <div className="space-y-6">
      {/* Meta */}
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-muted-foreground">
        <span>name: <span className="text-foreground/80">{String(data.name ?? "")}</span></span>
        <span>env: <span className="text-foreground/80">{String(data.env ?? "-")}</span></span>
        <span>kind: <span className="text-foreground/80">{String(data.kind ?? "-")}</span></span>
        <span>
          更新: <span className="text-foreground/80">{String(data.updated_at ?? "-")}</span>
        </span>
      </div>

      {data.summary ? (
        <p className="text-sm text-foreground/80">{String(data.summary)}</p>
      ) : null}

      {/* Triggers */}
      {triggers.length > 0 && (
        <section>
          <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold">
            <TriangleAlert className="size-4 text-muted-foreground" /> 触发条件
          </h3>
          <div className="flex flex-wrap gap-1.5">
            {triggers.map((t, i) => (
              <span key={i} className="rounded-md bg-muted px-2 py-0.5 text-xs">
                {String(t)}
              </span>
            ))}
          </div>
        </section>
      )}

      {/* Steps */}
      {steps.length > 0 && (
        <section>
          <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold">
            <ListChecks className="size-4 text-muted-foreground" /> 步骤（{steps.length}）
          </h3>
          <ol className="space-y-2">
            {steps.map((step, i) => (
              <li key={step.id as string ?? i} className="rounded-lg border border-border bg-card p-3">
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
                        className="overflow-x-auto rounded-md bg-muted/60 px-2.5 py-1.5 font-mono text-xs text-foreground/85"
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
                        verify: <code className="font-mono text-foreground/75">{String(step.verify)}</code>
                      </div>
                    ) : null}
                    {step.expect ? (
                      <div>
                        expect: <code className="font-mono text-foreground/75">{String(step.expect)}</code>
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </li>
            ))}
          </ol>
        </section>
      )}

      {/* Rollback */}
      {rollback.length > 0 && (
        <section>
          <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold">
            <Terminal className="size-4 text-muted-foreground" /> 回滚预案
          </h3>
          <div className="space-y-1.5">
            {rollback.map((rb, i) => (
              <div key={i} className="rounded-lg border border-border bg-card p-3">
                {rb.title ? <div className="mb-1 text-sm">{String(rb.title)}</div> : null}
                {Array.isArray(rb.commands) &&
                  rb.commands.map((cmd, j) => (
                    <pre
                      key={j}
                      className="overflow-x-auto rounded-md bg-muted/60 px-2.5 py-1.5 font-mono text-xs text-foreground/85"
                    >
                      {String(cmd)}
                    </pre>
                  ))}
                {rb.note ? <div className="mt-1 text-xs text-muted-foreground">{String(rb.note)}</div> : null}
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Anything else (sanitized, read-only) */}
      {Object.keys(data).some((k) => !known.has(k)) && (
        <section>
          <h3 className="mb-2 text-sm font-semibold">其他字段</h3>
          <div className="rounded-lg border border-border bg-card p-3">
            {Object.entries(data)
              .filter(([k]) => !known.has(k))
              .map(([k, v]) => (
                <div
                  key={k}
                  className="flex gap-2 border-b border-dotted border-border/60 py-1.5 last:border-b-0"
                >
                  <span className="min-w-32 shrink-0 text-muted-foreground">{k}</span>
                  <span className="min-w-0 flex-1">
                    <DetailValue value={v} />
                  </span>
                </div>
              ))}
          </div>
        </section>
      )}
    </div>
  );
}

function RunbookListItem({
  rb,
  selected,
  onSelect,
}: {
  rb: RunbookSummary;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "w-full rounded-lg border border-border bg-card p-3 text-left shadow-sm transition-colors hover:border-foreground/20 hover:bg-muted/40",
        selected && "border-foreground/30 bg-muted/50",
      )}
    >
      <div className="mb-1 flex flex-wrap items-center gap-2">
        <FileText className="size-4 shrink-0 text-muted-foreground" />
        <span className="font-medium">{rb.title}</span>
        <EnvTag env={rb.env} />
        <KindBadge kind={rb.kind} checklist={rb.checklist} />
      </div>
      {rb.summary && (
        <div className="mb-1 line-clamp-2 text-xs text-muted-foreground">{rb.summary}</div>
      )}
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span className="font-mono">{rb.name}</span>
        <span>· {rb.step_count} 步</span>
        {rb.triggers.length > 0 && <span>· {rb.triggers[0]}</span>}
        {rb.updated_at && (
          <span className="ml-auto inline-flex items-center gap-1">
            <Clock className="size-3" /> {String(rb.updated_at).slice(0, 10)}
          </span>
        )}
      </div>
    </button>
  );
}

export default function RunbooksPage() {
  const [list, setList] = useState<RunbookSummary[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
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
          <h1 className="text-lg font-semibold">Runbook 列表</h1>
        </div>
        <Button ghost size="sm" onClick={loadList} aria-label="刷新">
          <RefreshCw className="size-4" />
        </Button>
        <span className="ml-auto text-xs text-muted-foreground">
          只读 · 内容已脱敏（路径/密钥/URL userinfo 过滤）
        </span>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(300px,380px)_1fr]">
        {/* List */}
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
            <div className="flex max-h-[calc(100dvh-12rem)] flex-col gap-2 overflow-y-auto pr-1">
              {list.map((rb) => (
                <RunbookListItem
                  key={rb.name}
                  rb={rb}
                  selected={selected === rb.name}
                  onSelect={() => setSelected(rb.name)}
                />
              ))}
            </div>
          ) : (
            <Card className="border-dashed">
              <CardContent className="p-8 text-center text-sm text-muted-foreground">
                无 runbook 数据
              </CardContent>
            </Card>
          )}
        </div>

        {/* Detail */}
        <div>
          {!selected ? (
            <Card className="border-dashed">
              <CardContent className="flex items-center justify-center gap-2 p-10 text-sm text-muted-foreground">
                <ChevronRight className="size-4" /> 选择左侧 runbook 查看详情
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
            <Card className="shadow-sm">
              <CardContent className="p-5">
                <div className="mb-3 flex flex-wrap items-center gap-2">
                  <h2 className="text-base font-semibold">{String(detail.title ?? detail.name ?? "")}</h2>
                  <EnvTag env={String(detail.env ?? "")} />
                  <KindBadge kind={String(detail.kind ?? "")} checklist={Boolean(detail.checklist)} />
                </div>
                <RunbookDetailBody data={detail} />
              </CardContent>
            </Card>
          ) : null}
        </div>
      </div>
    </div>
  );
}
