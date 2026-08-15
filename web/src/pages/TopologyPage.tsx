import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import {
  ChevronDown,
  ChevronRight,
  Database,
  LayoutGrid,
  Layers,
  Link2,
  List,
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
import { api } from "@/lib/api";
import type {
  TopologyCard,
  TopologyHost,
  TopologyService,
  TopologyView,
} from "@/lib/api";
import { OpsKeyValueTree } from "@/components/OpsKeyValueTree";
import { TopologyMiniGraph } from "@/components/ops/TopologyMiniGraph";
import { cn } from "@/lib/utils";
import {
  STATUS_FILTERS,
  statusMatchesFilter,
  statusPillClass,
  statusTone,
  type StatusFilterId,
} from "@/lib/ops";

const ENV_COLORS: Record<string, string> = {
  local: "bg-sky-600",
  test: "bg-amber-600",
  dev: "bg-orange-600",
  prod: "bg-red-600",
};

function envClass(env?: string): string {
  return ENV_COLORS[(env ?? "").trim().toLowerCase()] ?? "bg-slate-500";
}

function EnvBadge({ env }: { env?: string }) {
  if (!env?.trim()) return null;
  return (
    <span className={cn("rounded px-1.5 py-px text-[10px] text-white", envClass(env))}>
      {env}
    </span>
  );
}

function StatusPill({ status }: { status?: string }) {
  if (!status?.trim()) return null;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-px text-[11px]",
        statusPillClass(status),
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
        <span key={k} className="rounded border border-border bg-muted/40 px-1.5 py-px text-[11px]">
          <span className="text-muted-foreground">{k}</span> {v}
        </span>
      ))}
    </div>
  );
}

function matchesCard(card: TopologyCard, q: string): boolean {
  if (!q) return true;
  return `${card.name} ${card.type ?? ""} ${card.env ?? ""}`.toLowerCase().includes(q);
}

function DetailBox({ detailKey, view }: { detailKey: string; view: TopologyView }) {
  const detail = view.details?.[detailKey];
  return (
    <div className="mt-2 rounded border border-border bg-muted/40 p-3">
      {detail ? (
        <OpsKeyValueTree data={detail} />
      ) : (
        <span className="text-sm text-muted-foreground">无详情数据</span>
      )}
    </div>
  );
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

function ServiceRow({
  svc,
  hostName,
  view,
  q,
  filter,
  openDetail,
  toggleDetail,
}: {
  svc: TopologyService;
  hostName: string;
  view: TopologyView;
  q: string;
  filter: StatusFilterId;
  openDetail: string | null;
  toggleDetail: (key: string) => void;
}) {
  const card = svc.card;
  if (!matchesCard(card, q)) return null;
  if (!statusMatchesFilter(card.status, filter)) return null;
  const detailKey = `service:${hostName}:${card.name}`;
  const open = openDetail === detailKey;
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-2 rounded px-2 py-1 text-sm",
        "odd:bg-muted/30",
        card.on_key_path && "ring-1 ring-inset ring-amber-500/40",
      )}
    >
      <span className="font-medium">{card.name}</span>
      {card.on_key_path && (
        <span className="text-amber-500" title="关键链路实体">
          <Link2 className="size-3.5" />
        </span>
      )}
      {card.type && <span className="text-xs text-muted-foreground">{card.type}</span>}
      {card.endpoint && (
        <span className="font-mono text-[11px] text-muted-foreground">{card.endpoint}</span>
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
  filter,
  openDetail,
  toggleDetail,
}: {
  host: TopologyHost;
  view: TopologyView;
  q: string;
  filter: StatusFilterId;
  openDetail: string | null;
  toggleDetail: (key: string) => void;
}) {
  const card = host.card;
  const detailKey = `host:${card.name}`;
  const open = openDetail === detailKey;

  const anyServiceVisible = host.services.some(
    (s) => matchesCard(s.card, q) && statusMatchesFilter(s.card.status, filter),
  );
  if (!matchesCard(card, q)) return null;
  if (!statusMatchesFilter(card.status, filter) && !anyServiceVisible) return null;

  const visibleServices = host.services.filter(
    (s) => matchesCard(s.card, q) && statusMatchesFilter(s.card.status, filter),
  );

  return (
    <Card className={cn("border-border bg-card", card.on_key_path && "border-amber-500/50")}>
      <CardContent className="p-3.5">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <Server className="size-4 text-muted-foreground" />
          <span className="font-semibold">{card.name}</span>
          <EnvBadge env={card.env} />
          <StatusPill status={card.status} />
          {card.on_key_path && (
            <span className="text-[11px] text-amber-500">
              <Link2 className="size-3" /> 关键链路
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
          <div className="mb-1 text-[11px] text-muted-foreground">
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
                filter={filter}
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
  filter,
  openDetail,
  toggleDetail,
}: {
  svc: TopologyService;
  view: TopologyView;
  q: string;
  filter: StatusFilterId;
  openDetail: string | null;
  toggleDetail: (key: string) => void;
}) {
  const card = svc.card;
  if (!matchesCard(card, q)) return null;
  if (!statusMatchesFilter(card.status, filter)) return null;
  const detailKey = `cross:${card.name}`;
  const open = openDetail === detailKey;
  return (
    <Card className={cn("border-border bg-card", card.on_key_path && "border-amber-500/50")}>
      <CardContent className="p-3.5">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <Layers className="size-4 text-muted-foreground" />
          <span className="font-semibold">{card.name}</span>
          <EnvBadge env={card.env} />
          <StatusPill status={card.status} />
          {card.on_key_path && (
            <span className="text-[11px] text-amber-500">
              <Link2 className="size-3" />
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

/** 高密度列表视图行：名称 / 类型 / env / endpoint / 状态。 */
function ListRow({ card, kind }: { card: TopologyCard; kind: string }) {
  const tone = statusTone(card.status);
  const toneLabel =
    tone === "ok" ? "正常" : tone === "warn" ? "告警" : tone === "error" ? "故障" : "离线";
  return (
    <div className="grid grid-cols-[1fr_auto_auto_auto_1fr_auto] items-center gap-2 border-b border-border/70 px-2 py-1.5 text-sm last:border-b-0 hover:bg-muted/30">
      <span className="flex items-center gap-1.5 truncate">
        {card.on_key_path && <Link2 className="size-3 shrink-0 text-amber-500" />}
        <span className="font-medium">{card.name}</span>
      </span>
      <span className="text-xs text-muted-foreground">{kind}</span>
      {card.type ? (
        <span className="text-xs text-muted-foreground">{card.type}</span>
      ) : (
        <span />
      )}
      <EnvBadge env={card.env} />
      <span className="truncate font-mono text-[11px] text-muted-foreground">
        {card.endpoint ?? "-"}
      </span>
      <span
        className={cn(
          "inline-flex items-center gap-1 rounded-full px-2 py-px text-[11px]",
          statusPillClass(card.status),
        )}
      >
        <span className="size-1.5 rounded-full bg-current" />
        {card.status || toneLabel}
      </span>
    </div>
  );
}

export default function TopologyPage() {
  const [searchParams] = useSearchParams();
  const [view, setView] = useState<TopologyView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState(() => searchParams.get("q") ?? "");
  const [filter, setFilter] = useState<StatusFilterId>("all");
  const [viewMode, setViewMode] = useState<"card" | "list">("card");
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
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <Network className="size-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">资产拓扑</h1>
        </div>
        <Button ghost size="sm" onClick={load} aria-label="刷新">
          <RefreshCw className="size-4" />
        </Button>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {/* Status filter */}
          <div className="flex items-center gap-0.5 rounded border border-border bg-card p-0.5">
            {STATUS_FILTERS.map((f) => (
              <button
                key={f.id}
                type="button"
                onClick={() => setFilter(f.id)}
                className={cn(
                  "rounded px-2 py-0.5 text-[11px] transition-colors",
                  filter === f.id
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-muted",
                )}
              >
                {f.label}
              </button>
            ))}
          </div>
          {/* View toggle */}
          <div className="flex items-center gap-0.5 rounded border border-border bg-card p-0.5">
            <button
              type="button"
              onClick={() => setViewMode("card")}
              aria-label="卡片视图"
              className={cn(
                "rounded p-1",
                viewMode === "card" ? "bg-muted text-foreground" : "text-muted-foreground",
              )}
            >
              <LayoutGrid className="size-3.5" />
            </button>
            <button
              type="button"
              onClick={() => setViewMode("list")}
              aria-label="列表视图"
              className={cn(
                "rounded p-1",
                viewMode === "list" ? "bg-muted text-foreground" : "text-muted-foreground",
              )}
            >
              <List className="size-3.5" />
            </button>
          </div>
          <div className="relative">
            <Search className="absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              className="h-8 w-56 pl-7 text-sm"
              placeholder="搜索 name / type / env…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
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
          <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <span>数据根：{view.data_root}</span>
            <span>{view.clusters.length} 集群</span>
            <span>{view.hosts.length} 主机</span>
            <span>{view.hosts.reduce((n, h) => n + h.services.length, 0)} 服务</span>
            <span>{view.cross_host.length} 跨主机实体</span>
            <span>{view.key_paths.length} 条关键链路</span>
          </div>

          {/* Key-path schematic */}
          <Card className="mb-4 border-border bg-card">
            <CardContent className="p-3">
              <div className="mb-1.5 flex items-center gap-2 text-xs font-medium text-muted-foreground">
                <Database className="size-3.5" />
                关键链路缩略图（琥珀点 = 链上服务）
              </div>
              <TopologyMiniGraph view={view} />
            </CardContent>
          </Card>

          {viewMode === "list" ? (
            <Card className="border-border bg-card">
              <CardContent className="p-2">
                {groups.map((group) => {
                  const visibleHosts = group.hosts.filter((host) => {
                    if (!matchesCard(host.card, q)) return false;
                    const svcVisible = host.services.some(
                      (s) => matchesCard(s.card, q) && statusMatchesFilter(s.card.status, filter),
                    );
                    return statusMatchesFilter(host.card.status, filter) || svcVisible;
                  });
                  if (visibleHosts.length === 0) return null;
                  return (
                    <div key={group.name} className="mb-2 last:mb-0">
                      <div className="flex items-center gap-2 px-2 py-1 text-xs font-semibold">
                        <Database className="size-3.5 text-muted-foreground" />
                        集群 {group.name}
                        <span className="font-normal text-muted-foreground">
                          （{visibleHosts.length} 台主机）
                        </span>
                      </div>
                      <div className="rounded border border-border/70">
                        {visibleHosts.map((host) => (
                          <div key={host.card.name}>
                            <ListRow card={host.card} kind="host" />
                            {host.services
                              .filter(
                                (s) =>
                                  matchesCard(s.card, q) &&
                                  statusMatchesFilter(s.card.status, filter),
                              )
                              .map((s) => (
                                <ListRow key={s.card.name} card={s.card} kind="service" />
                              ))}
                          </div>
                        ))}
                        {view.cross_host
                          .filter(
                            (c) =>
                              matchesCard(c.card, q) &&
                              statusMatchesFilter(c.card.status, filter),
                          )
                          .map((c) => (
                            <ListRow key={c.card.name} card={c.card} kind="cross_host" />
                          ))}
                      </div>
                    </div>
                  );
                })}
                {view.hosts.length === 0 && (
                  <p className="px-2 py-4 text-sm italic text-muted-foreground">
                    拓扑表暂无主机（hosts 段为空）。
                  </p>
                )}
              </CardContent>
            </Card>
          ) : (
            <>
              {groups.map((group) => (
                <section key={group.name} className="mb-6">
                  <h2 className="mb-2.5 flex flex-wrap items-baseline gap-2 text-sm font-semibold">
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
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                    {group.hosts.map((host) => (
                      <HostCard
                        key={host.card.name}
                        host={host}
                        view={view}
                        q={q}
                        filter={filter}
                        openDetail={openDetail}
                        toggleDetail={toggleDetail}
                      />
                    ))}
                  </div>
                </section>
              ))}

              {view.cross_host.length > 0 && (
                <section className="mb-6">
                  <h2 className="mb-2.5 flex items-center gap-2 text-sm font-semibold">
                    <Layers className="size-4 text-muted-foreground" />
                    跨主机实体（{view.cross_host.length}）
                  </h2>
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
                    {view.cross_host.map((svc) => (
                      <CrossCard
                        key={svc.card.name}
                        svc={svc}
                        view={view}
                        q={q}
                        filter={filter}
                        openDetail={openDetail}
                        toggleDetail={toggleDetail}
                      />
                    ))}
                  </div>
                </section>
              )}
            </>
          )}

          {/* Key paths */}
          {view.key_paths.length > 0 && (
            <section className="mt-6">
              <h2 className="mb-2.5 flex items-center gap-2 text-sm font-semibold">
                <Link2 className="size-4 text-muted-foreground" />
                关键链路（{view.key_paths.length}）
              </h2>
              <div className="space-y-2">
                {view.key_paths.map((chain, i) => (
                  <div key={i} className="flex flex-wrap items-center gap-1.5 text-sm">
                    {chain.map((name, j) => (
                      <span key={`${name}-${j}`} className="flex items-center gap-1.5">
                        {j > 0 && <span className="text-muted-foreground">→</span>}
                        <span className="rounded border border-amber-500/50 bg-amber-500/5 px-1.5 py-0.5 text-amber-600 dark:text-amber-400">
                          {name}
                        </span>
                      </span>
                    ))}
                  </div>
                ))}
              </div>
            </section>
          )}
        </>
      ) : null}
    </div>
  );
}
