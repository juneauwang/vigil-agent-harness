import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import { useTranslation } from "react-i18next";
import "@/i18n";
import {
  Database,
  LayoutGrid,
  Layers,
  Link2,
  List,
  Network,
  Search,
  Server,
  SquarePen,
  Trash2,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import type { TopologyCard, TopologyHost, TopologyService, TopologyView } from "@/lib/api";
import TopologyGraph from "@/components/TopologyGraph";
import DetailDrawer from "@/components/DetailDrawer";
import YamlEditorDrawer, { type YamlEditorTarget } from "@/components/YamlEditorDrawer";
import { EnvBadge, StatusPill } from "@/components/StatusBits";
import { cn, lastSeenInfo, matchesSearch, statusMatchesFilter, type StatusFilterId } from "@/lib/ops";
import type { GraphEntityRef } from "@/lib/topologyGraph";

const STATUS_FILTER_LABELS: Array<{ id: StatusFilterId; key: string }> = [
  { id: "all", key: "topology.filter.all" },
  { id: "ok", key: "topology.filter.ok" },
  { id: "warn", key: "topology.filter.warn" },
  { id: "error", key: "topology.filter.error" },
  { id: "offline", key: "topology.filter.offline" },
];

function DetailButton({ onClick }: { onClick: () => void }) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={onClick}
      className="vigil-btn h-6 gap-1 px-2 text-xs"
      aria-label={t("topology.detailAria")}
    >
      {t("topology.detailBtn")}
    </button>
  );
}

/** task27 PART A: 实体事实源 YAML 编辑入口（小图标按钮，行内/卡片头部）。 */
function EditYamlButton({ onClick }: { onClick: () => void }) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid="topo-edit-yaml"
      aria-label={t("yamlEditor.editAria")}
      title={t("yamlEditor.editAria")}
      className="vigil-btn h-6 px-1.5 text-xs"
    >
      <SquarePen className="size-3.5" />
    </button>
  );
}

/** Batch 35: host activity line (lazy last_seen; label provided by i18n). */
function ActivityLine({ lastSeen }: { lastSeen?: number }) {
  const { t } = useTranslation();
  const info = lastSeenInfo(lastSeen);
  return (
    <div className="mb-1.5 flex items-center gap-1.5 text-[11px] text-[var(--vigil-muted)]">
      <span
        className={cn(
          "size-1.5 rounded-full",
          info.tone === "ok" ? "bg-[var(--vigil-ok)]" : "bg-[var(--vigil-offline)]",
        )}
      />
      <span>{t("topology.activity", { label: info.label })}</span>
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
  hostName,
  q,
  filter,
  onDetail,
  onEdit,
}: {
  svc: TopologyService;
  /** 所属 host 名（服务事实源 entityId 需要三段：service:<host>:<name>）。 */
  hostName: string;
  q: string;
  filter: StatusFilterId;
  onDetail: (e: GraphEntityRef) => void;
  onEdit: (entityId: string, title: string) => void;
}) {
  const { t } = useTranslation();
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
        <span title={t("topology.keyPathTitle")}>
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
      <div className="ml-auto flex min-w-0 items-center gap-1.5">
        <EditYamlButton onClick={() => onEdit(`service:${hostName}:${card.name}`, card.name)} />
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
  onEdit,
}: {
  host: TopologyHost;
  q: string;
  filter: StatusFilterId;
  onDetail: (e: GraphEntityRef) => void;
  onEdit: (entityId: string, title: string) => void;
}) {
  const { t } = useTranslation();
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
    <div
      id={`topo-row-${card.name}`}
      className={cn("vigil-card cursor-pointer p-3.5", card.on_key_path && "border-amber-500/50")}
      onClick={() => onDetail({ kind: "host", name: card.name, card, detail: host.detail })}
    >
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Server className="size-4 text-[var(--vigil-muted)]" />
        <span className="font-semibold">{card.name}</span>
        <EnvBadge env={card.env} />
        <StatusPill status={card.status} />
        {card.on_key_path && (
          <span className="flex items-center gap-1 text-[11px] text-amber-500">
            <Link2 className="size-3" /> {t("topology.keyPath")}
          </span>
        )}
        <div className="ml-auto flex min-w-0 items-center gap-1.5">
          <EditYamlButton onClick={() => onEdit(`host:${card.name}`, card.name)} />
          <DetailButton onClick={() => onDetail({ kind: "host", name: card.name, card, detail: host.detail })} />
        </div>
      </div>
      <Facts card={card} />
      <ActivityLine lastSeen={card.last_seen} />
      <div className="border-t border-dashed border-[var(--vigil-border)] pt-2">
        <div className="mb-1 text-[11px] text-[var(--vigil-muted)]">
          {visibleServices.length > 0 ? t("topology.servicesWithCount", { n: visibleServices.length }) : t("topology.kind.service")}
        </div>
        {visibleServices.length === 0 ? (
          <div className="text-xs italic text-[var(--vigil-muted)]">{t("topology.noServices")}</div>
        ) : (
          visibleServices.map((svc) => (
            <ServiceRow
              key={svc.card.name}
              svc={svc}
              hostName={card.name}
              q={q}
              filter={filter}
              onDetail={onDetail}
              onEdit={onEdit}
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
  onEdit,
}: {
  svc: TopologyService;
  q: string;
  filter: StatusFilterId;
  onDetail: (e: GraphEntityRef) => void;
  onEdit: (entityId: string, title: string) => void;
}) {
  const card = svc.card;
  if (!matchesSearch(card, q)) return null;
  if (!statusMatchesFilter(card.status, filter)) return null;
  return (
    <div
      id={`topo-row-${card.name}`}
      className={cn("vigil-card cursor-pointer p-3.5", card.on_key_path && "border-amber-500/50")}
      onClick={() => onDetail({ kind: "cross_host", name: card.name, card, detail: svc.detail })}
    >
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Layers className="size-4 text-[var(--vigil-muted)]" />
        <span className="font-semibold">{card.name}</span>
        <EnvBadge env={card.env} />
        <StatusPill status={card.status} />
        {card.on_key_path && <Link2 className="size-3 text-amber-500" />}
        <div className="ml-auto flex min-w-0 items-center gap-1.5">
          <EditYamlButton onClick={() => onEdit(`cross_host:${card.name}`, card.name)} />
          <DetailButton onClick={() => onDetail({ kind: "cross_host", name: card.name, card, detail: svc.detail })} />
        </div>
      </div>
      <Facts card={card} />
    </div>
  );
}

/** High-density list-view row. */
function ListRow({
  card,
  kind,
  entityId,
  onFocus,
  onEdit,
}: {
  card: TopologyCard;
  kind: string;
  /** 事实源 entityId（host:<name> / service:<host>:<name> / cross_host:<name>）；空 = 不给编辑入口。 */
  entityId?: string;
  onFocus: (name: string) => void;
  onEdit?: (entityId: string, title: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <div
      id={`topo-row-${card.name}`}
      onClick={() => onFocus(card.name)}
      className="grid cursor-pointer grid-cols-[1fr_auto_auto_auto_1fr_auto_auto] items-center gap-2 border-b border-[var(--vigil-border)]/70 px-2 py-1.5 text-sm last:border-b-0 hover:bg-[var(--vigil-muted-bg)]"
    >
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
      <span className="justify-self-end">
        {entityId && onEdit ? (
          <button
            type="button"
            data-testid="topo-edit-yaml"
            onClick={(e) => {
              e.stopPropagation();
              onEdit(entityId, card.name);
            }}
            aria-label={t("yamlEditor.editAria")}
            title={t("yamlEditor.editAria")}
            className="rounded p-1 text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)]"
          >
            <SquarePen className="size-3.5" />
          </button>
        ) : null}
      </span>
    </div>
  );
}

export default function TopologyPage() {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  // task27 PART D: 顶栏全局搜索提交 → /topology?q=...。query 此前只在
  // useState 初始化器里读一次 URL 参数——组件已挂载时（再次提交搜索 /
  // 参数变化）新 q 永远进不了过滤器。订阅参数变化显式同步。
  const urlQuery = searchParams.get("q") ?? "";
  const [view, setView] = useState<TopologyView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState(() => searchParams.get("q") ?? "");
  // URL ?q= 变化（含顶栏搜索再提交 / 他页跳转）→ 覆盖页内搜索框并应用过滤。
  useEffect(() => {
    setQuery(urlQuery);
  }, [urlQuery]);
  const [filter, setFilter] = useState<StatusFilterId>("all");
  const [viewMode, setViewMode] = useState<"card" | "list">("card");
  const [drawer, setDrawer] = useState<GraphEntityRef | null>(null);
  // task27 PART A: raw YAML 编辑抽屉（host/service/cross 档案 + cluster→topology.yaml）。
  const [editTarget, setEditTarget] = useState<YamlEditorTarget | null>(null);
  const [resetting, setResetting] = useState(false);
  // Batch 49: graph ↔ list sync — clicking a graph node scrolls the list to
  // that row; clicking a list row highlights the node in the graph
  // (TopologyGraph focusedName).
  const [focused, setFocused] = useState<string | null>(null);

  useEffect(() => {
    if (!focused) return;
    const el = document.getElementById(`topo-row-${focused}`);
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [focused]);

  const loadTopology = () => {
    api
      .getTopology()
      .then((resp) => {
        if (resp.ok && resp.data) setView(resp.data);
      })
      .catch(() => {});
  };

  useEffect(() => {
    let alive = true;
    api
      .getTopology()
      .then((resp) => {
        if (!alive) return;
        if (resp.ok && resp.data) setView(resp.data);
        else setError(resp.error ?? t("topology.loadFailed"));
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, []);

  // task27 PART A: 打开实体事实源编辑抽屉；保存成功后重新拉取拓扑视图。
  const openYamlEditor = (entityId: string, title: string) => {
    setEditTarget({
      title,
      subtitle: entityId.startsWith("cluster:") ? "topology.yaml" : entityId,
      load: () => api.getTopologyEntityRawYaml(entityId),
      save: (text) => api.putTopologyEntityRawYaml(entityId, text),
    });
  };

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

  // v0.4: service dependency link count (services-level depends_on, replacing key_paths).
  const depCount = useMemo(() => {
    if (!view) return 0;
    const names = new Set(view.hosts.flatMap((h) => h.services.map((s) => s.card.name)));
    const seen = new Set<string>();
    let n = 0;
    for (const host of view.hosts) {
      for (const svc of host.services) {
        for (const dep of svc.card.depends_on ?? []) {
          if (dep === svc.card.name || !names.has(dep)) continue;
          const key = `${svc.card.name}->${dep}`;
          if (!seen.has(key)) {
            seen.add(key);
            n += 1;
          }
        }
      }
    }
    return n;
  }, [view]);

  // task27 B2: 搜索命中统计（语料 = 全部节点 name/type/env：clusters+hosts+services+cross）
  // + 第一个命中名（Enter 聚焦：列表滚动 + 图高亮 ring，是最廉价的聚焦路径）。
  const searchMatch = useMemo(() => {
    if (!view || !q) return null;
    let total = 0;
    let matched = 0;
    let first: string | null = null;
    const count = (card: { name: string; type?: string; env?: string }) => {
      total += 1;
      if (matchesSearch(card, q)) {
        matched += 1;
        if (first === null) first = card.name;
      }
    };
    for (const c of view.clusters) count({ name: c.name, env: c.env });
    for (const h of view.hosts) {
      count(h.card);
      for (const svc of h.services) count(svc.card);
    }
    for (const c of view.cross_host) count(c.card);
    return { total, matched, first };
  }, [view, q]);

  /** Batch 85: clear ALL topology data (destructive; confirm dialog + auditing owned by the backend). */
  const handleReset = async () => {
    if (!view) return;
    const ok = window.confirm(
      t("topology.resetConfirm", { hosts: view.hosts.length, clusters: view.clusters.length }),
    );
    if (!ok) return;
    setResetting(true);
    try {
      const resp = await api.resetTopology();
      if (resp.ok) {
        setView(null);
        setError(t("topology.resetDone"));
      } else {
        setError(typeof resp.error === "string" ? resp.error : t("topology.resetFailed"));
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setResetting(false);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Toolbar */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-2">
          <Network className="size-5 text-[var(--vigil-muted)]" />
          <h1 className="text-lg font-semibold">Topology</h1>
        </div>

        <div className="ml-auto flex flex-wrap items-center gap-2">
          {/* Status filter */}
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
                {t(f.key)}
              </button>
            ))}
          </div>

          {/* List/card switch */}
          <div className="flex items-center gap-0.5 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)] p-0.5">
            <button
              type="button"
              onClick={() => setViewMode("card")}
              aria-label={t("topology.cardViewAria")}
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
              aria-label={t("topology.listViewAria")}
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

          {/* Search（task27 B2: Enter 聚焦首个命中 + 清除按钮 + 命中数提示） */}
          <div className="flex h-8 w-56 items-center gap-2 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)] px-2.5 focus-within:border-[var(--vigil-primary)]">
            <Search className="size-3.5 shrink-0 text-[var(--vigil-muted)]" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && searchMatch?.first) setFocused(searchMatch.first);
                if (e.key === "Escape") setQuery("");
              }}
              placeholder={t("topology.searchPlaceholder")}
              data-testid="topology-search"
              className="h-full min-w-0 flex-1 bg-transparent text-sm text-[var(--vigil-text)] outline-none placeholder:text-[var(--vigil-muted)]/60"
            />
            {query ? (
              <button
                type="button"
                onClick={() => setQuery("")}
                aria-label={t("common.searchClearAria")}
                className="rounded p-0.5 text-[var(--vigil-muted)] hover:text-[var(--vigil-text)]"
              >
                <X className="size-3.5" />
              </button>
            ) : null}
          </div>

          {/* Batch 85: clear-topology entry (destructive, confirm dialog) */}
          <button
            type="button"
            onClick={() => void handleReset()}
            disabled={resetting}
            className="vigil-btn h-8 gap-1 px-2.5 text-xs"
            aria-label={t("topology.clearTopology")}
            title={t("topology.clearTitle")}
          >
            <Trash2 className="size-3.5" />
            {resetting ? t("topology.clearing") : t("topology.clearTopology")}
          </button>
        </div>
      </div>

      {error ? (
        <div className="vigil-card border-dashed p-10 text-center text-sm text-[var(--vigil-muted)]">
          {error}
        </div>
      ) : view ? (
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
          {/* Meta + key-path graph thumbnail */}
          <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-[var(--vigil-muted)]">
            <span>{t("topology.dataRoot", { root: view.data_root })}</span>
            <span>{t("topology.clustersCount", { n: view.clusters.length })}</span>
            <span>{t("topology.hostsCount", { n: view.hosts.length })}</span>
            <span>{t("topology.servicesCount", { n: view.hosts.reduce((n, h) => n + h.services.length, 0) })}</span>
            <span>{t("topology.crossCount", { n: view.cross_host.length })}</span>
            <span>{t("topology.depCount", { n: depCount })}</span>
            {searchMatch ? (
              <span
                data-testid="topo-search-hint"
                className={cn(
                  "rounded px-1.5 py-px text-[11px]",
                  searchMatch.matched === 0
                    ? "bg-[var(--vigil-muted-bg)] text-[var(--vigil-muted)]"
                    : "bg-[var(--vigil-muted-bg)] text-[var(--vigil-text)]",
                )}
              >
                {searchMatch.matched === 0
                  ? t("topology.searchEmpty", { query: query.trim() })
                  : t("topology.searchMatched", { matched: searchMatch.matched, total: searchMatch.total })}
              </span>
            ) : null}
          </div>

          <div className="vigil-card mb-4 p-3">
            <div
              className="mb-1.5 flex items-center gap-2 text-xs font-medium text-[var(--vigil-muted)]"
              title={t("topology.overviewTitleTip")}
            >
              <Database className="size-3.5" />
              {t("topology.overviewTitle")}
            </div>
            <TopologyGraph
              view={view}
              onSelect={setDrawer}
              onNodeFocus={setFocused}
              focusedName={focused}
              searchQuery={query}
            />
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
                      {t("topology.clusterHeading", { name: group.name })}
                      <StatusPill status={group.meta?.status} />
                      <span className="font-normal text-[var(--vigil-muted)]">
                        {t("topology.hostCountSuffix", { n: visibleHosts.length })}
                      </span>
                    </div>
                    <div className="rounded border border-[var(--vigil-border)]/70">
                      {visibleHosts.map((host) => (
                        <div key={host.card.name}>
                          <ListRow
                            card={host.card}
                            kind="host"
                            entityId={`host:${host.card.name}`}
                            onFocus={setFocused}
                            onEdit={openYamlEditor}
                          />
                          {host.services
                            .filter(
                              (s) =>
                                matchesSearch(s.card, q) &&
                                statusMatchesFilter(s.card.status, filter),
                            )
                            .map((s) => (
                              <ListRow
                                key={s.card.name}
                                card={s.card}
                                kind="service"
                                entityId={`service:${host.card.name}:${s.card.name}`}
                                onFocus={setFocused}
                                onEdit={openYamlEditor}
                              />
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
                          <ListRow
                            key={c.card.name}
                            card={c.card}
                            kind="cross_host"
                            entityId={`cross_host:${c.card.name}`}
                            onFocus={setFocused}
                            onEdit={openYamlEditor}
                          />
                        ))}
                    </div>
                  </div>
                );
              })}
              {view.hosts.length === 0 && (
                <p className="px-2 py-4 text-sm italic text-[var(--vigil-muted)]">
                  {t("topology.noHosts")}
                </p>
              )}
            </div>
          ) : (
            <>
              {groups.map((group) => (
                <section key={group.name} className="mb-6">
                  <h2 className="mb-2.5 flex flex-wrap items-baseline gap-2 text-sm font-semibold">
                    <Database className="size-4 text-[var(--vigil-muted)]" />
                    {t("topology.clusterHeading", { name: group.name })}
                    <StatusPill status={group.meta?.status} />
                    {group.meta?.description && (
                      <span className="text-xs font-normal text-[var(--vigil-muted)]">
                        — {group.meta.description}
                      </span>
                    )}
                    <span className="text-xs font-normal text-[var(--vigil-muted)]">
                      {t("topology.hostCountSuffix", { n: group.hosts.length })}
                    </span>
                    {/* task27 PART A: 集群事实源 = topology.yaml 整文件 */}
                    <EditYamlButton onClick={() => openYamlEditor(`cluster:${group.name}`, group.name)} />
                  </h2>
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                    {group.hosts.map((host) => (
                      <HostCard
                        key={host.card.name}
                        host={host}
                        q={q}
                        filter={filter}
                        onDetail={setDrawer}
                        onEdit={openYamlEditor}
                      />
                    ))}
                  </div>
                </section>
              ))}

              {view.cross_host.length > 0 && (
                <section className="mb-6">
                  <h2 className="mb-2.5 flex items-center gap-2 text-sm font-semibold">
                    <Layers className="size-4 text-[var(--vigil-muted)]" />
                    {t("topology.crossHeading", { n: view.cross_host.length })}
                  </h2>
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
                    {view.cross_host.map((svc) => (
                      <CrossCard
                        key={svc.card.name}
                        svc={svc}
                        q={q}
                        filter={filter}
                        onDetail={setDrawer}
                        onEdit={openYamlEditor}
                      />
                    ))}
                  </div>
                </section>
              )}
            </>
          )}

        </div>
      ) : null}

      <DetailDrawer entity={drawer} onClose={() => setDrawer(null)} />

      {/* task27 PART A: raw YAML 编辑抽屉（保存成功 → 重新拉取拓扑视图） */}
      <YamlEditorDrawer
        target={editTarget}
        onClose={() => setEditTarget(null)}
        onSaved={() => loadTopology()}
      />
    </div>
  );
}
