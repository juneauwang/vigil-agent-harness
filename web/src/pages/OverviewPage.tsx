import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import {
  Activity,
  ArrowRight,
  CheckCircle2,
  CircleDot,
  Clock,
  Cloud,
  Cpu,
  ListChecks,
  Server,
  ShieldAlert,
} from "lucide-react";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { api } from "@/lib/api";
import type { TopologyView, StatusResponse } from "@/lib/api";
import { TopologyMiniGraph } from "@/components/ops/TopologyMiniGraph";
import { EmptyState } from "@/components/ops/EmptyState";
import { cn } from "@/lib/utils";
import { statusTone } from "@/lib/ops";

function StatCard({
  icon,
  label,
  value,
  sub,
}: {
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
}) {
  return (
    <Card className="border-border bg-card">
      <CardContent className="flex items-center gap-3 p-3.5">
        <div className="flex size-8 shrink-0 items-center justify-center rounded-md border border-border bg-muted/50 text-muted-foreground">
          {icon}
        </div>
        <div className="min-w-0">
          <div className="text-[11px] text-muted-foreground">{label}</div>
          <div className="text-lg font-semibold leading-tight">{value}</div>
          {sub && <div className="truncate text-[11px] text-muted-foreground/80">{sub}</div>}
        </div>
      </CardContent>
    </Card>
  );
}

function StatusDot({ status }: { status?: string }) {
  const tone = statusTone(status);
  const color =
    tone === "ok"
      ? "bg-emerald-500"
      : tone === "warn"
        ? "bg-amber-500"
        : tone === "error"
          ? "bg-red-500"
          : "bg-slate-400";
  return <span className={cn("inline-block size-1.5 shrink-0 rounded-full", color)} />;
}

export default function OverviewPage() {
  const navigate = useNavigate();
  const [view, setView] = useState<TopologyView | null>(null);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    Promise.all([api.getTopology(), api.getStatus().catch(() => null)])
      .then(([topo, st]) => {
        if (!alive) return;
        if (topo.ok && topo.data) setView(topo.data);
        else setError(topo.error ?? "拓扑加载失败");
        setStatus(st);
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const stats = useMemo(() => {
    if (!view) return { clusters: 0, hosts: 0, services: 0 };
    return {
      clusters: view.clusters.length,
      hosts: view.hosts.length,
      services: view.hosts.reduce((n, h) => n + h.services.length, 0),
    };
  }, [view]);

  /** name → status, from host/service/cross-host cards, falling back to
   *  layer-3 details. Missing status → undefined (renders gray 未知). */
  const entityStatus = useMemo(() => {
    const map = new Map<string, string>();
    if (!view) return map;
    for (const h of view.hosts) {
      if (h.card.status) map.set(h.card.name, h.card.status);
      for (const s of h.services) {
        if (s.card.status) map.set(s.card.name, s.card.status);
      }
    }
    for (const c of view.cross_host) {
      if (c.card.status) map.set(c.card.name, c.card.status);
    }
    for (const [key, detail] of Object.entries(view.details ?? {})) {
      const name = key.includes(":") ? key.split(":").pop() : key;
      const st = (detail as Record<string, unknown>)?.status;
      if (typeof st === "string" && !map.has(name ?? "")) map.set(name ?? "", st);
    }
    return map;
  }, [view]);

  if (loading) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center gap-2 text-sm text-muted-foreground">
        <Spinner />
        <span>加载概览…</span>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-7xl p-4 lg:p-6">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <Activity className="size-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">概览</h1>
        </div>
        {view && (
          <span className="text-xs text-muted-foreground">
            数据根 {view.data_root} · {view.generated_at}
          </span>
        )}
        <span className="ml-auto text-xs text-muted-foreground">只读 · 凭据已过滤</span>
      </div>

      {error && !view ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
            <ShieldAlert className="size-7 text-muted-foreground" />
            <div className="text-sm text-muted-foreground">{error}</div>
          </CardContent>
        </Card>
      ) : (
        <>
          {/* Stat cards */}
          <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatCard icon={<Cloud className="size-4" />} label="集群" value={stats.clusters} />
            <StatCard icon={<Server className="size-4" />} label="主机" value={stats.hosts} />
            <StatCard icon={<Cpu className="size-4" />} label="服务" value={stats.services} />
            <StatCard
              icon={<ListChecks className="size-4" />}
              label="待审批"
              value={0}
              sub="审批 API 未就绪"
            />
          </div>

          {/* Topology thumbnail + key paths */}
          <div className="mb-4 grid grid-cols-1 gap-4 xl:grid-cols-[1fr_340px]">
            <Card className="border-border bg-card">
              <CardContent className="p-3">
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-xs font-medium text-muted-foreground">
                    轻量拓扑图（集群 → 主机 → 服务）
                  </span>
                  <button
                    type="button"
                    onClick={() => navigate("/topology")}
                    className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                  >
                    打开资产拓扑 <ArrowRight className="size-3" />
                  </button>
                </div>
                {view ? (
                  <TopologyMiniGraph view={view} />
                ) : (
                  <EmptyState title="无拓扑数据" hint="先运行 vigil topo-discover 发现主机" />
                )}
              </CardContent>
            </Card>

            <Card className="border-border bg-card">
              <CardContent className="p-3">
                <div className="mb-2 flex items-center gap-2 text-xs font-medium text-muted-foreground">
                  <CircleDot className="size-3.5" />
                  关键链路摘要（{view?.key_paths.length ?? 0}）
                </div>
                {view && view.key_paths.length > 0 ? (
                  <div className="flex flex-col gap-2.5">
                    {view.key_paths.map((chain, i) => (
                      <div key={i} className="flex flex-wrap items-center gap-1.5 text-xs">
                        {chain.map((name, j) => (
                          <span key={`${name}-${j}`} className="flex items-center gap-1.5">
                            {j > 0 && <span className="text-muted-foreground">→</span>}
                            <span
                              className={cn(
                                "inline-flex items-center gap-1.5 rounded-md border border-border bg-muted/40 px-1.5 py-0.5",
                              )}
                              title={status ? `status: ${status}` : undefined}
                            >
                              <StatusDot status={entityStatus.get(name)} />
                              {name}
                              {!entityStatus.get(name) && (
                                <span className="text-muted-foreground/70">未知</span>
                              )}
                            </span>
                          </span>
                        ))}
                      </div>
                    ))}
                    <p className="text-[11px] text-muted-foreground/80">
                      排障时最先查的主干；实体状态取拓扑卡片/详情，缺省灰色"未知"。
                    </p>
                  </div>
                ) : (
                  <EmptyState title="暂无关键链路" className="py-6" />
                )}
              </CardContent>
            </Card>
          </div>

          {/* Executions + approvals placeholders */}
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <div>
              <div className="mb-2 flex items-center gap-2 text-xs font-medium text-muted-foreground">
                <Clock className="size-3.5" />
                近期 Runbook 执行
              </div>
              <EmptyState
                title="暂无执行记录"
                description="执行历史 API 未就绪（Codex 批次落地执行核心后接入）。"
                hint="预留：执行日志页 / 常驻终端面板将展示同一数据源"
              />
            </div>
            <div>
              <div className="mb-2 flex items-center gap-2 text-xs font-medium text-muted-foreground">
                <CheckCircle2 className="size-3.5" />
                待审批
              </div>
              <EmptyState
                title="暂无待审批"
                description="审批 API 未就绪（第三批接入）。"
                hint="高风险变更将在这里按风险分级展示"
                action={
                  <button
                    type="button"
                    onClick={() => navigate("/approvals")}
                    className="text-xs text-primary hover:underline"
                  >
                    前往审批中心 →
                  </button>
                }
              />
            </div>
          </div>

          {/* Gateway/version strip */}
          {status && (
            <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <span>
                版本 <span className="text-foreground/80">{status.version}</span>
              </span>
              <span>
                活跃会话 <span className="text-foreground/80">{status.active_sessions ?? 0}</span>
              </span>
              <span>
                Gateway{" "}
                <span
                  className={cn(
                    "rounded-full px-1.5 py-0.5 text-[11px]",
                    status.gateway_running
                      ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
                      : "bg-slate-500/15 text-slate-500",
                  )}
                >
                  {status.gateway_state ?? (status.gateway_running ? "running" : "stopped")}
                </span>
              </span>
            </div>
          )}
        </>
      )}
    </div>
  );
}
