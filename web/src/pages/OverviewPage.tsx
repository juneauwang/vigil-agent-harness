import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { ArrowRight, Boxes, ListChecks, Server, TriangleAlert, X } from "lucide-react";
import { api } from "@/lib/api";
import type { RunbookCoverageResponse, RunbookSummary, TopologyView } from "@/lib/api";
import TopologyGraph from "@/components/TopologyGraph";
import DetailDrawer from "@/components/DetailDrawer";
import { EmptyState } from "@/components/EmptyState";
import type { GraphEntityRef } from "@/lib/topologyGraph";
import { cn } from "@/lib/ops";

/** 4 指标卡（Nodes sky / Services emerald / Runbooks amber / Incidents rose）。 */
function MetricCard({
  tone,
  icon,
  label,
  value,
  sub,
  onClick,
}: {
  tone: "sky" | "emerald" | "amber" | "rose" | "violet";
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  onClick?: () => void;
}) {
  const tones: Record<string, string> = {
    sky: "bg-sky-100 text-sky-600 dark:bg-sky-500/15 dark:text-sky-400",
    emerald: "bg-emerald-100 text-emerald-600 dark:bg-emerald-500/15 dark:text-emerald-400",
    amber: "bg-amber-100 text-amber-600 dark:bg-amber-500/15 dark:text-amber-400",
    rose: "bg-rose-100 text-rose-600 dark:bg-rose-500/15 dark:text-rose-400",
    violet: "bg-violet-100 text-violet-600 dark:bg-violet-500/15 dark:text-violet-400",
  };
  return (
    <div
      className={cn("vigil-card flex items-center gap-3 p-4", onClick && "cursor-pointer hover:border-[var(--vigil-primary)]/60")}
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={onClick ? (e) => { if (e.key === "Enter" || e.key === " ") onClick(); } : undefined}
    >
      <div className={cn("flex size-9 shrink-0 items-center justify-center rounded-md", tones[tone])}>
        {icon}
      </div>
      <div className="min-w-0">
        <div className="text-sm text-[var(--vigil-muted)]">{label}</div>
        <div className="text-xl font-semibold leading-tight">{value}</div>
        {sub && <div className="truncate text-[11px] text-[var(--vigil-muted)] opacity-80">{sub}</div>}
      </div>
    </div>
  );
}

/** Runbook Queue 悬浮抽屉（可关闭，行点击跳 Runbooks?name=）。 */
function RunbookQueue({
  runbooks,
  onClose,
  onSelect,
}: {
  runbooks: RunbookSummary[];
  onClose: () => void;
  onSelect: (name: string) => void;
}) {
  return (
    <div className="vigil-card relative w-full shrink-0 lg:w-[320px]">
      <div className="p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-medium">Runbook Queue</h3>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭 Runbook Queue"
            className="text-[var(--vigil-muted)] hover:text-[var(--vigil-text)]"
          >
            <X className="size-4" />
          </button>
        </div>

        {runbooks.length === 0 ? (
          <EmptyState
            title="暂无 Runbook"
            hint="先运行 vigil topo-discover 或创建 runbooks/*.yaml"
            className="py-8"
          />
        ) : (
          <div className="scroll-thin overflow-x-auto">
            <table className="vigil-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Type</th>
                  <th>Status</th>
                  <th className="text-right">更新</th>
                </tr>
              </thead>
              <tbody>
                {runbooks.map((rb) => (
                  <tr key={rb.name} onClick={() => onSelect(rb.name)}>
                    <td className="max-w-[130px] truncate font-medium" title={rb.title}>
                      {rb.title}
                    </td>
                    <td className="text-[var(--vigil-muted)]">{rb.kind ?? "runbook"}</td>
                    <td className="text-[var(--vigil-muted)]">{rb.checklist ? "checklist" : "-"}</td>
                    <td className="text-right text-[var(--vigil-muted)]">
                      {rb.updated_at ? String(rb.updated_at).slice(0, 10) : "-"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

export default function OverviewPage() {
  const navigate = useNavigate();
  const [view, setView] = useState<TopologyView | null>(null);
  const [runbooks, setRunbooks] = useState<RunbookSummary[]>([]);
  const [queueOpen, setQueueOpen] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<GraphEntityRef | null>(null);
  const [incidentCount, setIncidentCount] = useState<number | null>(null);
  const [coverage, setCoverage] = useState<RunbookCoverageResponse["data"] | null>(null);

  useEffect(() => {
    let alive = true;
    Promise.all([
      api.getTopology().catch(() => null),
      api.getRunbooks().catch(() => null),
      api.getIncidents({ limit: 1 }).catch(() => null),
      api.getRunbookCoverage().catch(() => null),
    ])
      .then(([topo, rbs, inc, cov]) => {
        if (!alive) return;
        if (topo && topo.ok && topo.data) setView(topo.data);
        else if (topo && !topo.ok) setError(topo.error ?? "拓扑加载失败");
        if (rbs && rbs.ok && rbs.data) setRunbooks(rbs.data.runbooks);
        if (inc) setIncidentCount(typeof inc.total === "number" ? inc.total : (inc.incidents?.length ?? 0));
        if (cov && cov.ok && cov.data) setCoverage(cov.data);
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, []);

  const stats = useMemo(() => {
    if (!view) return { nodes: 0, services: 0 };
    return {
      nodes: view.hosts.length,
      services: view.hosts.reduce((n, h) => n + h.services.length, 0),
    };
  }, [view]);

  const onSelectRunbook = (name: string) =>
    navigate(`/runbooks?name=${encodeURIComponent(name)}`);

  return (
    <div className="flex h-full min-h-0 flex-col gap-4">
      {error && !view && (
        <div className="vigil-card border-dashed p-6 text-center text-sm text-[var(--vigil-muted)]">
          {error}
        </div>
      )}

      {/* 5 指标卡行：Nodes / Services / Runbooks / Incidents（真实计数）/ 未覆盖风险 */}
      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-5">
        <MetricCard tone="sky" icon={<Boxes className="size-4" />} label="Nodes" value={stats.nodes} sub={view ? `clusters ${view.clusters.length}` : undefined} />
        <MetricCard tone="emerald" icon={<Server className="size-4" />} label="Services" value={stats.services} />
        <MetricCard tone="amber" icon={<ListChecks className="size-4" />} label="Runbooks" value={runbooks.length} sub="剧本库" />
        <MetricCard tone="rose" icon={<TriangleAlert className="size-4" />} label="Incidents" value={incidentCount ?? 0} sub={incidentCount === null ? "告警接入后显示" : "watch inbox"} />
        <MetricCard
          tone="violet"
          icon={<TriangleAlert className="size-4" />}
          label="未覆盖风险"
          value={coverage ? coverage.high_risk.uncovered.length : 0}
          sub={coverage ? `高危 ${coverage.high_risk.total} 已覆盖 ${coverage.high_risk.covered}（覆盖率 ${coverage.high_risk.coverage_pct}%）` : "矩阵 required 高危 − runbook 覆盖"}
          onClick={() => navigate("/runbooks")}
        />
      </div>

      {/* 中：Topology Graph 通栏 + Runbook Queue 抽屉 */}
      <div className="flex min-h-0 flex-1 flex-col gap-4 xl:flex-row">
        <div className="vigil-card flex min-h-[240px] flex-1 flex-col p-4">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-medium">Topology Graph</h3>
            <button
              type="button"
              onClick={() => navigate("/topology")}
              className="vigil-link inline-flex items-center gap-1 text-xs"
            >
              打开资产拓扑 <ArrowRight className="size-3" />
            </button>
          </div>
          {view ? (
            <div className="min-h-0 flex-1">
              <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-[var(--vigil-muted)]">
                <span className="flex items-center gap-1.5">
                  <span className="size-2.5 rounded-sm border border-[var(--vigil-primary)]/50 bg-[var(--vigil-card)]" />
                  集群
                </span>
                <span className="flex items-center gap-1.5">
                  <span className="size-2.5 rounded-sm border border-emerald-500/60 bg-emerald-500/10" />
                  主机
                </span>
                <span className="flex items-center gap-1.5">
                  <span className="size-2.5 rounded-sm border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)]" />
                  服务
                </span>
                <span className="flex items-center gap-1.5">
                  <span className="size-2.5 rounded-sm border border-amber-500/60 bg-amber-500/10" />
                  服务依赖
                </span>
              </div>
              <TopologyGraph view={view} onSelect={setDrawer} className="h-full min-h-[420px]" />
            </div>
          ) : (
            <EmptyState
              title="无拓扑数据"
              hint="先运行 vigil topo-discover 发现主机"
              className="min-h-[180px] flex-1"
            />
          )}
        </div>

        {queueOpen && (
          <RunbookQueue
            runbooks={runbooks}
            onClose={() => setQueueOpen(false)}
            onSelect={onSelectRunbook}
          />
        )}
      </div>

      <DetailDrawer entity={drawer} onClose={() => setDrawer(null)} />
    </div>
  );
}
