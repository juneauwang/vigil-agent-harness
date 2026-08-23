import { lazy, Suspense, useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router";
import {
  Bell,
  History,
  Grid3x3,
  LayoutDashboard,
  MessageSquare,
  Moon,
  Network,
  PanelLeftClose,
  PanelLeftOpen,
  ScrollText,
  Search,
  Settings,
  ShieldCheck,
  Sun,
  TriangleAlert,
} from "lucide-react";
import { api, type HealthResponse, type TopologyResponse } from "@/lib/api";
import { cn, formatUptime } from "@/lib/ops";
import ApprovalModal from "@/components/ApprovalModal";
import { useApprovalPolling, useApprovalSnapshot } from "@/lib/approvalPoller";

const OverviewPage = lazy(() => import("@/pages/OverviewPage"));
const TopologyPage = lazy(() => import("@/pages/TopologyPage"));
const RunbooksPage = lazy(() => import("@/pages/RunbooksPage"));
const MatrixPage = lazy(() => import("@/pages/MatrixPage"));
const IncidentsPage = lazy(() => import("@/pages/IncidentsPage"));
const ApprovalsPage = lazy(() => import("@/pages/ApprovalsPage"));
const AuditPage = lazy(() => import("@/pages/AuditPage"));
const ChatPage = lazy(() => import("@/pages/ChatPage"));

interface NavItem {
  path: string;
  label: string;
  icon: typeof LayoutDashboard;
}

/** 豆包菜单 8 项：8 个路由页。 */
const NAV_ITEMS: NavItem[] = [
  { path: "/chat", label: "Chat", icon: MessageSquare },
  { path: "/overview", label: "Overview", icon: LayoutDashboard },
  { path: "/topology", label: "Topology", icon: Network },
  { path: "/runbooks", label: "Runbooks", icon: ScrollText },
  { path: "/matrix", label: "Matrix", icon: Grid3x3 },
  { path: "/incidents", label: "Incidents", icon: TriangleAlert },
  { path: "/approvals", label: "Approvals", icon: ShieldCheck },
  { path: "/audit", label: "Audit", icon: History },
];

const THEME_KEY = "vigil-console-theme";

function useConsoleTheme() {
  const [dark, setDark] = useState(() => {
    try {
      return localStorage.getItem(THEME_KEY) === "dark";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    try {
      localStorage.setItem(THEME_KEY, dark ? "dark" : "light");
    } catch { /* ignore */ }
  }, [dark]);
  const toggle = useCallback(() => setDark((v) => !v), []);
  return { dark, toggle };
}

export default function App() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { dark, toggle } = useConsoleTheme();

  // 顶栏数据：环境标签（首个 cluster）+ API 健康 + 运行时长
  const [headerMeta, setHeaderMeta] = useState<{ env: string; apiOk: boolean; uptime: string }>({
    env: "",
    apiOk: true,
    uptime: "-",
  });
  useEffect(() => {
    let alive = true;
    Promise.all([api.getHealth().catch(() => null), api.getTopology().catch(() => null)])
      .then(([health, topo]) => {
        if (!alive) return;
        const h = health as HealthResponse | null;
        const t = topo as TopologyResponse | null;
        setHeaderMeta({
          apiOk: h?.ok !== false,
          uptime: formatUptime(h?.uptime_seconds),
          env:
            t && t.ok && t.data && t.data.clusters.length > 0 ? t.data.clusters[0].name : "",
        });
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  // 待审批数量（顶部徽标 + 铃铛角标，批三十四：接全局轮询实时化）
  const approvalPoll = useApprovalPolling();

  // 侧边栏：默认 60px 纯图标，可展开 200px
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem("vigil-sidebar-collapsed") !== "0";
    } catch {
      return true;
    }
  });
  const toggleCollapsed = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("vigil-sidebar-collapsed", next ? "1" : "0");
      } catch { /* ignore */ }
      return next;
    });
  }, []);

  // 全局搜索（回车跳拓扑带 q）
  const [globalQuery, setGlobalQuery] = useState("");
  const goSearch = useCallback(
    (e: FormEvent) => {
      e.preventDefault();
      const q = globalQuery.trim();
      navigate(q ? `/topology?q=${encodeURIComponent(q)}` : "/topology");
    },
    [globalQuery, navigate],
  );

  return (
    <div className="flex h-dvh min-h-0 flex-col overflow-hidden">
      {/* 顶部 Header（h-14，豆包布局） */}
      <header className="flex h-14 shrink-0 items-center gap-3 border-b border-[var(--vigil-border)] bg-[var(--vigil-card)] px-4">
        <div className="flex w-[220px] shrink-0 items-center gap-2">
          <button type="button" onClick={() => navigate("/overview")} className="flex items-center gap-2" aria-label="Vigil">
            <img src="/favicon.png" alt="Vigil" className="size-5 rounded" />
            <span className="text-base font-semibold" style={{ color: "var(--vigil-primary)" }}>
              Vigil
            </span>
          </button>
          {headerMeta.env && (
            <span className="hidden text-sm text-[var(--vigil-muted)] md:inline">{headerMeta.env}</span>
          )}
        </div>

        <form onSubmit={goSearch} className="relative hidden w-full max-w-md flex-1 sm:block">
          <div className="flex h-8 items-center gap-2 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)] px-3 focus-within:border-[var(--vigil-primary)]">
            <Search className="size-3.5 shrink-0 text-[var(--vigil-muted)]" />
            <input
              value={globalQuery}
              onChange={(e) => setGlobalQuery(e.target.value)}
              placeholder="搜索主机 / 服务 / 命令…"
              className="h-full min-w-0 flex-1 bg-transparent text-sm text-[var(--vigil-text)] outline-none placeholder:text-[var(--vigil-muted)]/60"
            />
          </div>
        </form>

        <div className="ml-auto flex items-center gap-3 text-sm">
          {/* API Healthy */}
          <span
            className={cn("vigil-badge hidden lg:inline-flex", headerMeta.apiOk ? "text-[var(--vigil-ok)]" : "text-[var(--vigil-error)]")}
            title="服务健康检查"
          >
            <span className={cn("vigil-status-dot", headerMeta.apiOk ? "dot-ok" : "dot-error")} />
            API {headerMeta.apiOk ? "Healthy" : "Degraded"}
          </span>

          {/* 待审批数（点击跳审批中心） */}
          <button
            type="button"
            onClick={() => navigate("/approvals")}
            title="待审批（点击进入审批中心）"
            className="vigil-badge hidden lg:inline-flex hover:bg-[var(--vigil-muted-bg)]"
          >
            Approvals {approvalPoll.total}
          </button>

          {/* 运行时长 */}
          <span className="vigil-badge hidden lg:inline-flex">Agent upt: {headerMeta.uptime}</span>

          {/* 明暗切换 */}
          <button
            type="button"
            onClick={toggle}
            aria-label={dark ? "切换浅色模式" : "切换深色模式"}
            title={dark ? "切换浅色模式" : "切换深色模式"}
            className="flex size-8 items-center justify-center rounded-md text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]"
          >
            {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
          </button>

          {/* 待审批通知（铃铛 + 数量角标 + 下拉列表，可跳审批中心） */}
          <ApprovalBell />

          {/* 设置菜单（主题 + 版本，无上游继承页入口） */}
          <SettingsMenu dark={dark} onToggleTheme={toggle} />
        </div>
      </header>

      <div className="flex min-h-0 min-w-0 flex-1">
        {/* 左侧窄侧边栏（默认 60px 纯图标，可展开 200px） */}
        <aside
          className={cn(
            "flex shrink-0 flex-col border-r border-[var(--vigil-border)] bg-[var(--vigil-card)] py-3",
            collapsed ? "w-[60px]" : "w-[200px]",
          )}
        >
          <nav className="flex flex-1 flex-col items-center gap-1">
            {NAV_ITEMS.map((item) => {
              const active = pathname === item.path || pathname.startsWith(item.path + "/");
              const Icon = item.icon;
              return (
                <NavLink
                  key={item.path}
                  to={item.path}
                  title={collapsed ? item.label : undefined}
                  onClick={() => {
                    // 点当前页导航项 = 回到概览（或保持收起态交互）
                    if (active) navigate("/overview");
                  }}
                  className={cn(
                    "flex h-10 items-center rounded-md transition-colors",
                    collapsed ? "w-10 justify-center" : "w-[176px] gap-2 px-2.5",
                    active
                      ? "bg-[var(--vigil-primary)]/10 text-[var(--vigil-primary)]"
                      : "text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)]",
                  )}
                >
                  <Icon className="size-[18px] shrink-0" />
                  {!collapsed && <span className="truncate text-sm">{item.label}</span>}
                </NavLink>
              );
            })}
          </nav>

          <div className="flex justify-center pt-2">
            <button
              type="button"
              onClick={toggleCollapsed}
              aria-label={collapsed ? "展开侧边栏" : "折叠侧边栏"}
              className="flex size-8 items-center justify-center rounded-md text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]"
            >
              {collapsed ? <PanelLeftOpen className="size-4" /> : <PanelLeftClose className="size-4" />}
            </button>
          </div>
        </aside>

        {/* 主内容区 */}
        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            <Suspense
              fallback={
                <div className="flex min-h-[40vh] items-center justify-center text-sm text-[var(--vigil-muted)]">
                  加载中…
                </div>
              }
            >
              <Routes>
                <Route path="/" element={<Navigate to="/overview" replace />} />
                <Route path="/chat" element={<ChatPage />} />
                <Route path="/overview" element={<OverviewPage />} />
                <Route path="/topology" element={<TopologyPage />} />
                <Route path="/runbooks" element={<RunbooksPage />} />
                <Route path="/matrix" element={<MatrixPage />} />
                <Route path="/incidents" element={<IncidentsPage />} />
                <Route path="/approvals" element={<ApprovalsPage />} />
                <Route path="/audit" element={<AuditPage />} />
                <Route path="*" element={<Navigate to="/overview" replace />} />
              </Routes>
            </Suspense>
          </div>
        </main>
      </div>

      {/* 审批全局弹窗（批三十四）：任何路由可见；决策独立于对话页审批卡 */}
      <ApprovalModal />
    </div>
  );
}

/** 顶部设置菜单：主题切换 + 版本信息（无上游继承页入口）。 */
/** 待审批通知：铃铛 + 数量角标；点开下拉列出待审批项，可跳审批中心。 */
function ApprovalBell() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  // 批三十四：铃铛角标/下拉接全局轮询快照，实时反映 pending 审批。
  const poll = useApprovalSnapshot();
  const count = poll.total;
  const items = poll.approvals.slice(0, 5);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: globalThis.MouseEvent) => {
      const el = ref.current;
      if (el && !el.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label="待审批通知"
        title={`待审批 ${count} 项`}
        aria-expanded={open}
        className="relative flex size-8 items-center justify-center rounded-md text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]"
      >
        <Bell className="size-4" />
        {count > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-1 text-[9px] font-semibold text-white">
            {count > 99 ? "99+" : count}
          </span>
        )}
      </button>
      {open && (
        <div className="absolute right-0 top-full z-50 mt-1 w-80 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)] p-1 shadow-[var(--vigil-shadow)]">
          <div className="px-2.5 py-1.5 text-[11px] font-medium tracking-wide text-[var(--vigil-muted)]">
            待审批（{count}）
          </div>
          {items.length === 0 ? (
            <div className="px-2.5 py-3 text-center text-xs text-[var(--vigil-muted)]">暂无待审批</div>
          ) : (
            items.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => {
                  navigate("/approvals");
                  setOpen(false);
                }}
                className="flex w-full flex-col gap-0.5 rounded px-2.5 py-1.5 text-left hover:bg-[var(--vigil-muted-bg)]"
              >
                <span className="truncate font-mono text-xs text-[var(--vigil-text)]">{item.command}</span>
                <span className="text-[11px] text-[var(--vigil-muted)]">
                  {item.env ? `${item.env} · ` : ""}
                  {item.grade ? `L${item.grade} · ` : ""}
                  {item.id}
                </span>
              </button>
            ))
          )}
          <div className="mt-1 border-t border-[var(--vigil-border)] pt-1">
            <button
              type="button"
              onClick={() => {
                navigate("/approvals");
                setOpen(false);
              }}
              className="vigil-link w-full px-2.5 py-1 text-left text-xs"
            >
              进入审批中心 →
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function SettingsMenu({ dark, onToggleTheme }: { dark: boolean; onToggleTheme: () => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const [version, setVersion] = useState<string>("-");

  useEffect(() => {
    api.getStatus().then((st) => {
      if (st.version) setVersion(st.version);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: globalThis.MouseEvent) => {
      const el = ref.current;
      if (el && !el.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label="设置菜单"
        aria-expanded={open}
        title="设置（主题 / 版本）"
        className="flex size-8 items-center justify-center rounded-md text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]"
      >
        <Settings className="size-4" />
      </button>
      {open && (
        <div className="absolute right-0 top-full z-50 mt-1 w-52 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)] p-1 shadow-[var(--vigil-shadow)]">
          <div className="px-2.5 py-1.5 text-[11px] font-medium tracking-wide text-[var(--vigil-muted)]">
            设置
          </div>
          <MenuRow
            label={dark ? "主题：深色（点击切换浅色）" : "主题：浅色（点击切换深色）"}
            onClick={() => {
              onToggleTheme();
              setOpen(false);
            }}
          />
          <MenuRow label={`版本 ${version}`} onClick={() => setOpen(false)} />
        </div>
      )}
    </div>
  );
}

function MenuRow({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex w-full items-center rounded px-2.5 py-1.5 text-left text-sm text-[var(--vigil-text)] hover:bg-[var(--vigil-muted-bg)]"
    >
      {label}
    </button>
  );
}
