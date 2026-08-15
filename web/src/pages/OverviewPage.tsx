import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import {
  ArrowRight,
  Boxes,
  ListChecks,
  Server,
  TriangleAlert,
  X,
} from "lucide-react";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { api } from "@/lib/api";
import type { TopologyView, RunbookSummary } from "@/lib/api";
import { TopologyMiniGraph } from "@/components/ops/TopologyMiniGraph";
import { EmptyState } from "@/components/ops/EmptyState";
import { cn } from "@/lib/utils";

function MetricCard({
  iconClass,
  icon,
  label,
  value,
  sub,
}: {
  iconClass: string;
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
}) {
  return (
    <Card className="border-border bg-card shadow-[0_1px_3px_rgba(0,0,0,0.06)]">
      <CardContent className="flex items-center gap-3 p-4">
        <div
          className={cn(
            "flex size-9 shrink-0 items-center justify-center rounded-md",
            iconClass,
          )}
        >
          {icon}
        </div>
        <div className="min-w-0">
          <div className="text-sm text-muted-foreground">{label}</div>
          <div className="text-xl font-semibold leading-tight">{value}</div>
          {sub && <div className="truncate text-[11px] text-muted-foreground/80">{sub}</div>}
        </div>
      </CardContent>
    </Card>
  );
}

/** Runbook Queue 悬浮抽屉（可关闭，行点击跳 Runbook 详情）。 */
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
    <Card className="relative w-full shrink-0 border-border bg-card shadow-[0_1px_3px_rgba(0,0,0,0.06)] lg:w-[320px]">
      <CardContent className="p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-medium">Runbook Queue</h3>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭 Runbook Queue"
            className="text-muted-foreground hover:text-foreground"
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
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-border text-left text-[11px] text-muted-foreground">
                  <th className="py-1.5 pr-2 font-medium">Name</th>
                  <th className="py-1.5 pr-2 font-medium">Type</th>
                  <th className="py-1.5 pr-2 font-medium">Status</th>
                  <th className="py-1.5 text-right font-medium">更新</th>
                </tr>
              </thead>
              <tbody>
                {runbooks.map((rb) => (
                  <tr
                    key={rb.name}
                    onClick={() => onSelect(rb.name)}
                    className="cursor-pointer border-b border-border/60 text-xs last:border-b-0 hover:bg-muted/40"
                  >
                    <td className="max-w-[130px] truncate py-1.5 pr-2 font-medium" title={rb.title}>
                      {rb.title}
                    </td>
                    <td className="py-1.5 pr-2 text-muted-foreground">{rb.kind ?? "runbook"}</td>
                    <td className="py-1.5 pr-2 text-muted-foreground">
                      {rb.checklist ? "checklist" : "-"}
                    </td>
                    <td className="py-1.5 text-right text-muted-foreground">
                      {rb.updated_at ? String(rb.updated_at).slice(0, 10) : "-"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function OverviewPage() {
  const navigate = useNavigate();
  const [view, setView] = useState<TopologyView | null>(null);
  const [runbooks, setRunbooks] = useState<RunbookSummary[]>([]);
  const [queueOpen, setQueueOpen] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    Promise.all([
      api.getTopology().catch(() => null),
      api.getRunbooks().catch(() => null),
    ])
      .then(([topo, rbs]) => {
        if (!alive) return;
        if (topo && topo.ok && topo.data) setView(topo.data);
        else if (topo && !topo.ok) setError(topo.error ?? "拓扑加载失败");
        if (rbs && rbs.ok && rbs.data) setRunbooks(rbs.data.runbooks);
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
    if (!view) return { nodes: 0, services: 0 };
    return {
      nodes: view.hosts.length,
      services: view.hosts.reduce((n, h) => n + h.services.length, 0),
    };
  }, [view]);

  const onSelectRunbook = (name: string) => navigate(`/runbooks?name=${encodeURIComponent(name)}`);

  if (loading) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center gap-2 text-sm text-muted-foreground">
        <Spinner />
        <span>加载概览…</span>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-4">
      {error && !view && (
        <Card className="border-dashed">
          <CardContent className="p-6 text-center text-sm text-muted-foreground">{error}</CardContent>
        </Card>
      )}

      {/* 4 指标卡行（Nodes sky / Services emerald / Runbooks amber / Incidents rose） */}
      <div className="grid grid-cols-2 gap-4 xl:grid-cols-4">
        <MetricCard
          iconClass="bg-sky-100 text-sky-600 dark:bg-sky-500/15 dark:text-sky-400"
          icon={<Boxes className="size-4" />}
          label="Nodes"
          value={stats.nodes}
          sub={view ? `clusters ${view.clusters.length}` : undefined}
        />
        <MetricCard
          iconClass="bg-emerald-100 text-emerald-600 dark:bg-emerald-500/15 dark:text-emerald-400"
          icon={<Server className="size-4" />}
          label="Services"
          value={stats.services}
        />
        <MetricCard
          iconClass="bg-amber-100 text-amber-600 dark:bg-amber-500/15 dark:text-amber-400"
          icon={<ListChecks className="size-4" />}
          label="Runbooks"
          value={runbooks.length}
          sub="剧本库"
        />
        <MetricCard
          iconClass="bg-rose-100 text-rose-600 dark:bg-rose-500/15 dark:text-rose-400"
          icon={<TriangleAlert className="size-4" />}
          label="Incidents"
          value={0}
          sub="事件 API 未就绪"
        />
      </div>

      {/* 中：Topology Graph 通栏 + Runbook Queue 抽屉 */}
      <div className="flex min-h-0 flex-1 flex-col gap-4 xl:flex-row">
        <Card className="min-h-[240px] flex-1 border-border bg-card shadow-[0_1px_3px_rgba(0,0,0,0.06)]">
          <CardContent className="flex h-full flex-col p-4">
            <div className="mb-3 flex items-center justify-between">
              <h3 className="text-sm font-medium">Topology Graph</h3>
              <button
                type="button"
                onClick={() => navigate("/topology")}
                className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
              >
                打开资产拓扑 <ArrowRight className="size-3" />
              </button>
            </div>
            {view ? (
              <div className="min-h-0 flex-1">
                <TopologyMiniGraph view={view} />
              </div>
            ) : (
              <EmptyState
                title="无拓扑数据"
                hint="先运行 vigil topo-discover 发现主机"
                className="min-h-[180px] flex-1"
              />
            )}
          </CardContent>
        </Card>

        {queueOpen && (
          <RunbookQueue
            runbooks={runbooks}
            onClose={() => setQueueOpen(false)}
            onSelect={onSelectRunbook}
          />
        )}
      </div>
    </div>
  );
}
