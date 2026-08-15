import { useEffect, useMemo, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Database,
  Layers,
  Link2,
  Network,
  RefreshCw,
  Search,
  Server,
  ShieldAlert,
} from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Input } from "@nous-research/ui/ui/components/input";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { api } from "@/lib/api";
import type {
  TopologyCard,
  TopologyHost,
  TopologyService,
  TopologyView,
} from "@/lib/api";
import { OpsKeyValueTree } from "@/components/OpsKeyValueTree";
import { cn } from "@/lib/utils";

const ENV_COLORS: Record<string, string> = {
  local: "bg-sky-600",
  test: "bg-amber-600",
  dev: "bg-orange-600",
  prod: "bg-red-600",
};

function envClass(env?: string): string {
  return ENV_COLORS[(env ?? "").trim().toLowerCase()] ?? "bg-slate-500";
}

const STATUS_OK = new Set(["running", "up", "healthy", "ok"]);
const STATUS_DOWN = new Set(["stopped", "down", "offline", "error", "failed"]);

function statusClass(status?: string): string {
  const s = (status ?? "").trim().toLowerCase();
  if (STATUS_OK.has(s)) return "bg-emerald-500/15 text-emerald-400";
  if (STATUS_DOWN.has(s)) return "bg-slate-500/15 text-slate-400";
  return "bg-amber-500/15 text-amber-400";
}

function EnvBadge({ env }: { env?: string }) {
  if (!env?.trim()) return null;
  return (
    <Badge tone="default" className={cn("border-transparent text-white", envClass(env))}>
      {env}
    </Badge>
  );
}

function StatusPill({ status }: { status?: string }) {
  if (!status?.trim()) return null;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs",
        statusClass(status),
      )}
    >
      ● {status}
    </span>
  );
}

function Facts({ card }: { card: TopologyCard }) {
  const facts: Array<[string, string]> = [];
  for (const key of ["endpoint", "role", "runtime", "port", "owner", "type"] as const) {
    const v = card[key];
    if (v !== undefined && v !== null && String(v).trim() !== "") {
      facts.push([key, String(v)]);
    }
  }
  if (facts.length === 0) return null;
  return (
    <div className="mb-3 flex flex-wrap gap-1.5">
      {facts.map(([k, v]) => (
        <span
          key={k}
          className="rounded-md bg-muted px-2 py-0.5 text-xs text-foreground/80"
        >
          <span className="text-muted-foreground">{k}</span> {v}
        </span>
      ))}
    </div>
  );
}

/** Search matcher: name / type / env, case-insensitive substring. */
function matchesCard(card: TopologyCard, q: string): boolean {
  if (!q) return true;
  const hay = `${card.name} ${card.type ?? ""} ${card.env ?? ""}`.toLowerCase();
  return hay.includes(q);
}

function DetailToggle({
  open,
  onToggle,
  hasDetail,
  label,
}: {
  open: boolean;
  onToggle: () => void;
  hasDetail: boolean;
  label: string;
}) {
  if (!hasDetail) return null;
  return (
    <Button
      ghost
      size="sm"
      className="h-6 gap-1 px-2 text-xs text-foreground/70"
      onClick={onToggle}
      aria-expanded={open}
    >
      {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
      {open ? "收起" : label}
    </Button>
  );
}

function DetailBox({ detailKey, view }: { detailKey: string; view: TopologyView }) {
  const detail = view.details?.[detailKey];
  return (
    <div className="mt-2 rounded-lg border border-border bg-muted/40 p-3">
      {detail ? (
        <OpsKeyValueTree data={detail} />
      ) : (
        <span className="text-sm text-muted-foreground">无详情数据</span>
      )}
    </div>
  );
}

function ServiceRow({
  svc,
  hostName,
  view,
  q,
  openDetail,
  toggleDetail,
}: {
  svc: TopologyService;
  hostName: string;
  view: TopologyView;
  q: string;
  openDetail: string | null;
  toggleDetail: (key: string) => void;
}) {
  const card = svc.card;
  if (!matchesCard(card, q)) return null;
  const detailKey = `service:${hostName}:${card.name}`;
  const open = openDetail === detailKey;
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-2 rounded-lg px-2 py-1.5 text-sm",
        "odd:bg-muted/50",
        card.on_key_path && "ring-1 ring-inset ring-amber-500/50",
      )}
    >
      <span className="font-medium">{card.name}</span>
      {card.on_key_path && (
        <span className="text-amber-500" title="关键链路实体">
          <Link2 className="size-3.5" />
        </span>
      )}
      {card.type && (
        <span className="rounded-md bg-muted px-1.5 py-0.5 text-xs text-foreground/70">
          {card.type}
        </span>
      )}
      {card.endpoint && (
        <span className="font-mono text-xs text-muted-foreground">{card.endpoint}</span>
      )}
      <StatusPill status={card.status} />
      <DetailToggle
        open={open}
        onToggle={() => toggleDetail(detailKey)}
        hasDetail={Boolean(svc.detail)}
        label="详情"
      />
      {open && <DetailBox detailKey={detailKey} view={view} />}
    </div>
  );
}

function HostCard({
  host,
  view,
  q,
  openDetail,
  toggleDetail,
}: {
  host: TopologyHost;
  view: TopologyView;
  q: string;
  openDetail: string | null;
  toggleDetail: (key: string) => void;
}) {
  const card = host.card;
  const detailKey = `host:${card.name}`;
  const open = openDetail === detailKey;

  // Search: the host card shows when the host itself matches, or any of its
  // services match (service rows are filtered individually below).
  const anyServiceMatches = host.services.some((svc) => matchesCard(svc.card, q));
  if (!matchesCard(card, q) && !anyServiceMatches) return null;

  const visibleServices = host.services.filter((svc) => matchesCard(svc.card, q));

  return (
    <Card
      className={cn(
        "shadow-sm transition-shadow hover:shadow-md",
        card.on_key_path && "border-amber-500/60",
      )}
    >
      <CardContent className="p-4">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <Server className="size-4 text-muted-foreground" />
          <span className="font-semibold">{card.name}</span>
          <EnvBadge env={card.env} />
          <StatusPill status={card.status} />
          {card.on_key_path && (
            <span className="text-xs text-amber-500" title="关键链路实体">
              <Link2 className="size-3.5" /> 关键链路
            </span>
          )}
          <div className="ml-auto">
            <DetailToggle
              open={open}
              onToggle={() => toggleDetail(detailKey)}
              hasDetail={Boolean(host.detail)}
              label="主机详情"
            />
          </div>
        </div>
        <Facts card={card} />
        <div className="border-t border-dashed border-border pt-2">
          <div className="mb-1.5 text-xs text-muted-foreground">
            服务{visibleServices.length > 0 ? `（${visibleServices.length}）` : ""}
          </div>
          {visibleServices.length === 0 ? (
            <div className="text-xs italic text-muted-foreground">无服务数据</div>
          ) : (
            visibleServices.map((svc) => (
              <ServiceRow
                key={svc.card.name}
                svc={svc}
                hostName={card.name}
                view={view}
                q={q}
                openDetail={openDetail}
                toggleDetail={toggleDetail}
              />
            ))
          )}
        </div>
        {open && <DetailBox detailKey={detailKey} view={view} />}
      </CardContent>
    </Card>
  );
}

function CrossCard({
  svc,
  view,
  q,
  openDetail,
  toggleDetail,
}: {
  svc: TopologyService;
  view: TopologyView;
  q: string;
  openDetail: string | null;
  toggleDetail: (key: string) => void;
}) {
  const card = svc.card;
  if (!matchesCard(card, q)) return null;
  const detailKey = `cross:${card.name}`;
  const open = openDetail === detailKey;
  return (
    <Card
      className={cn(
        "shadow-sm transition-shadow hover:shadow-md",
        card.on_key_path && "border-amber-500/60",
      )}
    >
      <CardContent className="p-4">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <Layers className="size-4 text-muted-foreground" />
          <span className="font-semibold">{card.name}</span>
          <EnvBadge env={card.env} />
          <StatusPill status={card.status} />
          {card.on_key_path && (
            <span className="text-xs text-amber-500">
              <Link2 className="size-3.5" />
            </span>
          )}
          <div className="ml-auto">
            <DetailToggle
              open={open}
              onToggle={() => toggleDetail(detailKey)}
              hasDetail={Boolean(svc.detail)}
              label="详情"
            />
          </div>
        </div>
        <Facts card={card} />
        {open && <DetailBox detailKey={detailKey} view={view} />}
      </CardContent>
    </Card>
  );
}

export default function TopologyPage() {
  const [view, setView] = useState<TopologyView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [openDetail, setOpenDetail] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    setError(null);
    api
      .getTopology()
      .then((resp) => {
        if (resp.ok && resp.data) {
          setView(resp.data);
        } else {
          setView(null);
          setError(resp.error ?? "拓扑加载失败");
        }
      })
      .catch((e: unknown) => {
        setView(null);
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const toggleDetail = (key: string) =>
    setOpenDetail((prev) => (prev === key ? null : key));

  const q = query.trim().toLowerCase();

  const groups = useMemo(() => {
    if (!view) return [];
    const byCluster = new Map<string, TopologyHost[]>();
    for (const host of view.hosts) {
      const cluster = host.card.cluster || "default";
      const list = byCluster.get(cluster) ?? [];
      list.push(host);
      byCluster.set(cluster, list);
    }
    const meta = new Map(view.clusters.map((c) => [c.name, c]));
    const order = [...view.clusters.map((c) => c.name)];
    for (const name of byCluster.keys()) {
      if (!order.includes(name)) order.push(name);
    }
    return order
      .filter((name) => byCluster.has(name))
      .map((name) => ({
        name,
        meta: meta.get(name),
        hosts: byCluster.get(name)!,
      }));
  }, [view]);

  if (loading) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center gap-2 text-sm text-muted-foreground">
        <Spinner />
        <span>加载拓扑…</span>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-7xl p-4 lg:p-6">
      {/* Header */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <Network className="size-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">拓扑视图</h1>
        </div>
        <Button ghost size="sm" onClick={load} aria-label="刷新">
          <RefreshCw className="size-4" />
        </Button>
        <div className="ml-auto flex flex-wrap items-center gap-3">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              className="w-64 pl-8"
              placeholder="搜索 name / type / env…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <span className="text-xs text-muted-foreground">凭据已过滤 · 只读</span>
        </div>
      </div>

      {error ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
            <ShieldAlert className="size-8 text-muted-foreground" />
            <div className="text-sm text-muted-foreground">{error}</div>
            <Button size="sm" onClick={load}>
              重试
            </Button>
          </CardContent>
        </Card>
      ) : view ? (
        <>
          {/* Meta strip */}
          <div className="mb-5 rounded-lg border border-border bg-card p-3 text-xs text-muted-foreground shadow-sm">
            <div className="mb-1 flex flex-wrap gap-x-4 gap-y-1">
              <span>
                数据根：<span className="text-foreground/80">{view.data_root}</span>
              </span>
              <span>生成时间：{view.generated_at}</span>
              <span>
                数据来源：
                {view.sources && view.sources.length > 0 ? view.sources.join("、") : "-"}
              </span>
              <span>
                环境：
                {view.environments && view.environments.length > 0
                  ? view.environments.join("、")
                  : "-"}
              </span>
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              <span>{view.clusters.length} 集群</span>
              <span>{view.hosts.length} 主机</span>
              <span>{view.hosts.reduce((n, h) => n + h.services.length, 0)} 服务</span>
              <span>{view.cross_host.length} 跨主机实体</span>
              <span>{view.key_paths.length} 条关键链路</span>
              <span className="ml-auto text-muted-foreground/70">schema v{view.version ?? "?"}</span>
            </div>
          </div>

          {/* Cluster sections */}
          {groups.map((group) => (
            <section key={group.name} className="mb-8">
              <h2 className="mb-3 flex flex-wrap items-baseline gap-2 text-base font-semibold">
                <Database className="size-4 text-muted-foreground" />
                集群 {group.name}
                {group.meta?.description && (
                  <span className="text-xs font-normal text-muted-foreground">
                    — {group.meta.description}
                  </span>
                )}
                <span className="text-xs font-normal text-muted-foreground">
                  （{group.hosts.length} 台主机）
                </span>
              </h2>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
                {group.hosts.map((host) => (
                  <HostCard
                    key={host.card.name}
                    host={host}
                    view={view}
                    q={q}
                    openDetail={openDetail}
                    toggleDetail={toggleDetail}
                  />
                ))}
              </div>
            </section>
          ))}

          {view.hosts.length === 0 && (
            <p className="text-sm italic text-muted-foreground">
              拓扑表暂无主机（hosts 段为空）。
            </p>
          )}

          {/* Cross-host entities */}
          {view.cross_host.length > 0 && (
            <section className="mb-8">
              <h2 className="mb-3 flex items-center gap-2 text-base font-semibold">
                <Layers className="size-4 text-muted-foreground" />
                跨主机实体（{view.cross_host.length}）
              </h2>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
                {view.cross_host.map((svc) => (
                  <CrossCard
                    key={svc.card.name}
                    svc={svc}
                    view={view}
                    q={q}
                    openDetail={openDetail}
                    toggleDetail={toggleDetail}
                  />
                ))}
              </div>
            </section>
          )}

          {/* Key paths */}
          {view.key_paths.length > 0 && (
            <section className="mb-8">
              <h2 className="mb-3 flex items-center gap-2 text-base font-semibold">
                <Link2 className="size-4 text-muted-foreground" />
                关键链路（{view.key_paths.length}）
              </h2>
              <div className="space-y-2.5">
                {view.key_paths.map((chain, i) => (
                  <div key={i} className="flex flex-wrap items-center gap-2 text-sm">
                    {chain.map((name, j) => (
                      <span key={`${name}-${j}`} className="flex items-center gap-2">
                        {j > 0 && <span className="text-muted-foreground">→</span>}
                        <span
                          className={cn(
                            "rounded-md border border-amber-500/50 px-2 py-0.5 text-amber-600 dark:text-amber-400",
                            view.key_path_entity_names?.includes(name) && "bg-amber-500/10",
                          )}
                        >
                          {name}
                        </span>
                      </span>
                    ))}
                  </div>
                ))}
              </div>
              <p className="mt-2 text-xs text-muted-foreground">
                链上实体在页面中以琥珀色边框高亮。
              </p>
            </section>
          )}
        </>
      ) : null}
    </div>
  );
}
