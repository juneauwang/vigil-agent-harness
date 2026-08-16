import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import {
  Database,
  LayoutGrid,
  Layers,
  Link2,
  List,
  Network,
  Search,
  Server,
} from "lucide-react";
import { api } from "@/lib/api";
import type { TopologyCard, TopologyHost, TopologyService, TopologyView } from "@/lib/api";
import TopologyGraph from "@/components/TopologyGraph";
import DetailDrawer from "@/components/DetailDrawer";
import { EnvBadge, StatusPill } from "@/components/StatusBits";
import { cn, lastSeenInfo, matchesSearch, statusMatchesFilter, type StatusFilterId } from "@/lib/ops";
import type { GraphEntityRef } from "@/lib/topologyGraph";

const STATUS_FILTER_LABELS: Array<{ id: StatusFilterId; label: string }> = [
  { id: "all", label: "全部" },
  { id: "ok", label: "正常" },
  { id: "warn", label: "告警" },
  { id: "error", label: "故障" },
  { id: "offline", label: "离线" },
];

function DetailButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="vigil-btn h-6 gap-1 px-2 text-xs"
      aria-label="查看详情"
    >
      详情
    </button>
  );
}

/** 批三十五：host 活性行（lazy last_seen；"在线 · X 分钟前活跃" / "未探测"）。 */
function ActivityLine({ lastSeen }: { lastSeen?: number }) {
  const info = lastSeenInfo(lastSeen);
  return (
    <div className="mb-1.5 flex items-center gap-1.5 text-[11px] text-[var(--vigil-muted)]">
      <span
        className={cn(
          "size-1.5 rounded-full",
          info.tone === "ok" ? "bg-[var(--vigil-ok)]" : "bg-[var(--vigil-offline)]",
        )}
      />
      <span>活性：{info.label}</span>
    </div>
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
  if (card.ports && card.ports.length > 0) {
    facts.push(["ports", card.ports.join(", ")]);
  }
  if (facts.length === 0) return null;
  return (
    <div className="mb-3 flex flex-wrap gap-1.5">
      {facts.map(([k, v]) => (
        <span
          key={k}
          className="max-w-[260px] truncate whitespace-nowrap rounded border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)] px-1.5 py-px text-[11px]"
          title={v}
        >
          <span className="text-[var(--vigil-muted)]">{k}</span> {v}
        </span>
      ))}
    </div>
  );
}

function ServiceRow({
  svc,
  q,
  filter,
  onDetail,
}: {
  svc: TopologyService;
  q: string;
  filter: StatusFilterId;
  onDetail: (e: GraphEntityRef) => void;
}) {
  const card = svc.card;
  if (!matchesSearch(card, q)) return null;
  if (!statusMatchesFilter(card.status, filter)) return null;
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-2 rounded px-2 py-1 text-sm",
        "odd:bg-[var(--vigil-muted-bg)]",
        card.on_key_path && "ring-1 ring-inset ring-amber-500/40",
      )}
    >
      <span className="font-medium">{card.name}</span>
      {card.on_key_path && (
        <span title="关键链路实体">
          <Link2 className="size-3.5 text-amber-500" />
        </span>
      )}
      {card.type && <span className="text-xs text-[var(--vigil-muted)]">{card.type}</span>}
      {card.endpoint && (
        <span className="font-mono text-[11px] text-[var(--vigil-muted)]">{card.endpoint}</span>
      )}
      {card.ports && card.ports.length > 0 && (
        <span className="font-mono text-[11px] text-[var(--vigil-muted)]">
          ports {card.ports.join(", ")}
        </span>
      )}
      <StatusPill status={card.status} />
      <div className="ml-auto min-w-0">
        <DetailButton onClick={() => onDetail({ kind: "service", name: card.name, card, detail: svc.detail })} />
      </div>
    </div>
  );
}

function HostCard({
  host,
  q,
  filter,
  onDetail,
}: {
  host: TopologyHost;
  q: string;
  filter: StatusFilterId;
  onDetail: (e: GraphEntityRef) => void;
}) {
  const card = host.card;
  const anyServiceVisible = host.services.some(
    (s) => matchesSearch(s.card, q) && statusMatchesFilter(s.card.status, filter),
  );
  if (!matchesSearch(card, q)) return null;
  if (!statusMatchesFilter(card.status, filter) && !anyServiceVisible) return null;
  const visibleServices = host.services.filter(
    (s) => matchesSearch(s.card, q) && statusMatchesFilter(s.card.status, filter),
  );

  return (
    <div className={cn("vigil-card p-3.5", card.on_key_path && "border-amber-500/50")}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Server className="size-4 text-[var(--vigil-muted)]" />
        <span className="font-semibold">{card.name}</span>
        <EnvBadge env={card.env} />
        <StatusPill status={card.status} />
        {card.on_key_path && (
          <span className="flex items-center gap-1 text-[11px] text-amber-500">
            <Link2 className="size-3" /> 关键链路
          </span>
        )}
        <div className="ml-auto min-w-0">
          <DetailButton onClick={() => onDetail({ kind: "host", name: card.name, card, detail: host.detail })} />
        </div>
      </div>
      <Facts card={card} />
      <ActivityLine lastSeen={card.last_seen} />
      <div className="border-t border-dashed border-[var(--vigil-border)] pt-2">
        <div className="mb-1 text-[11px] text-[var(--vigil-muted)]">
          服务{visibleServices.length > 0 ? `（${visibleServices.length}）` : ""}
        </div>
        {visibleServices.length === 0 ? (
          <div className="text-xs italic text-[var(--vigil-muted)]">无服务数据</div>
        ) : (
          visibleServices.map((svc) => (
            <ServiceRow
              key={svc.card.name}
              svc={svc}
              q={q}
              filter={filter}
              onDetail={onDetail}
            />
          ))
        )}
      </div>
    </div>
  );
}

function CrossCard({
  svc,
  q,
  filter,
  onDetail,
}: {
  svc: TopologyService;
  q: string;
  filter: StatusFilterId;
  onDetail: (e: GraphEntityRef) => void;
}) {
  const card = svc.card;
  if (!matchesSearch(card, q)) return null;
  if (!statusMatchesFilter(card.status, filter)) return null;
  return (
    <div className={cn("vigil-card p-3.5", card.on_key_path && "border-amber-500/50")}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Layers className="size-4 text-[var(--vigil-muted)]" />
        <span className="font-semibold">{card.name}</span>
        <EnvBadge env={card.env} />
        <StatusPill status={card.status} />
        {card.on_key_path && <Link2 className="size-3 text-amber-500" />}
        <div className="ml-auto min-w-0">
          <DetailButton onClick={() => onDetail({ kind: "cross_host", name: card.name, card, detail: svc.detail })} />
        </div>
      </div>
      <Facts card={card} />
    </div>
  );
}

/** 高密度列表视图行。 */
function ListRow({ card, kind }: { card: TopologyCard; kind: string }) {
  return (
    <div className="grid grid-cols-[1fr_auto_auto_auto_1fr_auto] items-center gap-2 border-b border-[var(--vigil-border)]/70 px-2 py-1.5 text-sm last:border-b-0 hover:bg-[var(--vigil-muted-bg)]">
      <span className="flex items-center gap-1.5 truncate">
        {card.on_key_path && <Link2 className="size-3 shrink-0 text-amber-500" />}
        <span className="font-medium">{card.name}</span>
      </span>
      <span className="text-xs text-[var(--vigil-muted)]">{kind}</span>
      {card.type ? (
        <span className="text-xs text-[var(--vigil-muted)]">{card.type}</span>
      ) : (
        <span />
      )}
      <EnvBadge env={card.env} />
      {kind === "host" && card.last_seen ? (
        <span className="text-[11px] text-[var(--vigil-muted)]">{lastSeenInfo(card.last_seen).label}</span>
      ) : (
        <span className="truncate font-mono text-[11px] text-[var(--vigil-muted)]">
          {card.endpoint ?? "-"}
        </span>
      )}
      <StatusPill status={card.status} />
    </div>
  );
}

export default function TopologyPage() {
  const [searchParams] = useSearchParams();
  const [view, setView] = useState<TopologyView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState(() => searchParams.get("q") ?? "");
  const [filter, setFilter] = useState<StatusFilterId>("all");
  const [viewMode, setViewMode] = useState<"card" | "list">("card");
  const [drawer, setDrawer] = useState<GraphEntityRef | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .getTopology()
      .then((resp) => {
        if (!alive) return;
        if (resp.ok && resp.data) setView(resp.data);
        else setError(resp.error ?? "拓扑加载失败");
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, []);

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
      .map((name) => ({ name, meta: meta.get(name), hosts: byCluster.get(name)! }));
  }, [view]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* 工具条 */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-2">
          <Network className="size-5 text-[var(--vigil-muted)]" />
          <h1 className="text-lg font-semibold">Topology</h1>
        </div>

        <div className="ml-auto flex flex-wrap items-center gap-2">
          {/* 状态筛选 */}
          <div className="flex items-center gap-0.5 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)] p-0.5">
            {STATUS_FILTER_LABELS.map((f) => (
              <button
                key={f.id}
                type="button"
                onClick={() => setFilter(f.id)}
                className={cn(
                  "rounded px-2 py-0.5 text-[11px] transition-colors",
                  filter === f.id
                    ? "bg-[var(--vigil-primary)] text-white"
                    : "text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]",
                )}
              >
                {f.label}
              </button>
            ))}
          </div>

          {/* 列表/卡片切换 */}
          <div className="flex items-center gap-0.5 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)] p-0.5">
            <button
              type="button"
              onClick={() => setViewMode("card")}
              aria-label="卡片视图"
              className={cn(
                "rounded p-1",
                viewMode === "card"
                  ? "bg-[var(--vigil-muted-bg)] text-[var(--vigil-text)]"
                  : "text-[var(--vigil-muted)]",
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
                viewMode === "list"
                  ? "bg-[var(--vigil-muted-bg)] text-[var(--vigil-text)]"
                  : "text-[var(--vigil-muted)]",
              )}
            >
              <List className="size-3.5" />
            </button>
          </div>

          {/* 搜索 */}
          <div className="flex h-8 w-56 items-center gap-2 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)] px-2.5 focus-within:border-[var(--vigil-primary)]">
            <Search className="size-3.5 shrink-0 text-[var(--vigil-muted)]" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索名称 / 类型 / 环境"
              className="h-full min-w-0 flex-1 bg-transparent text-sm text-[var(--vigil-text)] outline-none placeholder:text-[var(--vigil-muted)]/60"
            />
          </div>
        </div>
      </div>

      {error ? (
        <div className="vigil-card border-dashed p-10 text-center text-sm text-[var(--vigil-muted)]">
          {error}
        </div>
      ) : view ? (
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
          {/* Meta + 关键链路缩略图 */}
          <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-[var(--vigil-muted)]">
            <span>数据根：{view.data_root}</span>
            <span>{view.clusters.length} 集群</span>
            <span>{view.hosts.length} 主机</span>
            <span>{view.hosts.reduce((n, h) => n + h.services.length, 0)} 服务</span>
            <span>{view.cross_host.length} 跨主机实体</span>
            <span>{view.key_paths.length} 条关键链路</span>
          </div>

          <div className="vigil-card mb-4 p-3">
            <div
              className="mb-1.5 flex items-center gap-2 text-xs font-medium text-[var(--vigil-muted)]"
              title="业务请求链路：入口 → 网关 → 服务 → 存储；琥珀点 = 链上服务"
            >
              <Database className="size-3.5" />
              拓扑总览（琥珀点 = 关键链路服务）
            </div>
            <TopologyGraph view={view} onSelect={setDrawer} />
          </div>

          {viewMode === "list" ? (
            <div className="vigil-card p-2">
              {groups.map((group) => {
                const visibleHosts = group.hosts.filter((host) => {
                  if (!matchesSearch(host.card, q)) return false;
                  const svcVisible = host.services.some(
                    (s) => matchesSearch(s.card, q) && statusMatchesFilter(s.card.status, filter),
                  );
                  return statusMatchesFilter(host.card.status, filter) || svcVisible;
                });
                if (visibleHosts.length === 0) return null;
                return (
                  <div key={group.name} className="mb-2 last:mb-0">
                    <div className="flex items-center gap-2 px-2 py-1 text-xs font-semibold">
                      <Database className="size-3.5 text-[var(--vigil-muted)]" />
                      集群 {group.name}
                      <StatusPill status={group.meta?.status} />
                      <span className="font-normal text-[var(--vigil-muted)]">
                        （{visibleHosts.length} 台主机）
                      </span>
                    </div>
                    <div className="rounded border border-[var(--vigil-border)]/70">
                      {visibleHosts.map((host) => (
                        <div key={host.card.name}>
                          <ListRow card={host.card} kind="host" />
                          {host.services
                            .filter(
                              (s) =>
                                matchesSearch(s.card, q) &&
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
                            matchesSearch(c.card, q) &&
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
                <p className="px-2 py-4 text-sm italic text-[var(--vigil-muted)]">
                  拓扑表暂无主机（hosts 段为空）。
                </p>
              )}
            </div>
          ) : (
            <>
              {groups.map((group) => (
                <section key={group.name} className="mb-6">
                  <h2 className="mb-2.5 flex flex-wrap items-baseline gap-2 text-sm font-semibold">
                    <Database className="size-4 text-[var(--vigil-muted)]" />
                    集群 {group.name}
                    <StatusPill status={group.meta?.status} />
                    {group.meta?.description && (
                      <span className="text-xs font-normal text-[var(--vigil-muted)]">
                        — {group.meta.description}
                      </span>
                    )}
                    <span className="text-xs font-normal text-[var(--vigil-muted)]">
                      （{group.hosts.length} 台主机）
                    </span>
                  </h2>
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                    {group.hosts.map((host) => (
                      <HostCard
                        key={host.card.name}
                        host={host}
                        q={q}
                        filter={filter}
                        onDetail={setDrawer}
                      />
                    ))}
                  </div>
                </section>
              ))}

              {view.cross_host.length > 0 && (
                <section className="mb-6">
                  <h2 className="mb-2.5 flex items-center gap-2 text-sm font-semibold">
                    <Layers className="size-4 text-[var(--vigil-muted)]" />
                    跨主机实体（{view.cross_host.length}）
                  </h2>
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
                    {view.cross_host.map((svc) => (
                      <CrossCard key={svc.card.name} svc={svc} q={q} filter={filter} onDetail={setDrawer} />
                    ))}
                  </div>
                </section>
              )}
            </>
          )}

          {/* 关键链路（业务请求链路：入口 → 网关 → 服务 → 存储） */}
          {view.key_paths.length > 0 && (
            <section className="mt-6">
              <h2
                className="mb-1 flex items-center gap-2 text-sm font-semibold"
                title="业务请求链路：入口 → 网关 → 服务 → 存储（定义在拓扑表 key_paths）"
              >
                <Link2 className="size-4 text-[var(--vigil-muted)]" />
                关键链路（{view.key_paths.length}）
              </h2>
              <p className="mb-2.5 text-xs text-[var(--vigil-muted)]">
                业务请求的主干路径——排障时最先检查这里。
              </p>
              <div className="space-y-2">
                {view.key_paths.map((chain, i) => (
                  <div key={i} className="flex flex-wrap items-center gap-1.5 text-sm">
                    {chain.map((name, j) => (
                      <span key={`${name}-${j}`} className="flex items-center gap-1.5">
                        {j > 0 && <span className="text-[var(--vigil-muted)]">→</span>}
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
        </div>
      ) : null}

      <DetailDrawer entity={drawer} onClose={() => setDrawer(null)} />
    </div>
  );
}
