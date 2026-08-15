import { lazy, Suspense, useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router";
import {
  Bell,
  History,
  LayoutDashboard,
  Moon,
  Network,
  PanelLeftClose,
  PanelLeftOpen,
  ScrollText,
  Search,
  Settings,
  ShieldCheck,
  Sun,
  Terminal,
  TriangleAlert,
} from "lucide-react";
import { api, type HealthResponse, type TopologyResponse } from "@/lib/api";
import { cn, formatUptime } from "@/lib/ops";
import { TerminalPanel } from "@/components/TerminalPanel";

const OverviewPage = lazy(() => import("@/pages/OverviewPage"));
const TopologyPage = lazy(() => import("@/pages/TopologyPage"));
const RunbooksPage = lazy(() => import("@/pages/RunbooksPage"));
const IncidentsPage = lazy(() => import("@/pages/IncidentsPage"));
const ApprovalsPage = lazy(() => import("@/pages/ApprovalsPage"));
const AuditPage = lazy(() => import("@/pages/AuditPage"));
const TerminalPage = lazy(() => import("@/pages/TerminalPage"));

interface NavItem {
  path: string;
  label: string;
  icon: typeof LayoutDashboard;
}

/** 豆包菜单 7 项：6 个路由页 + Terminal（锚定底部终端面板）。 */
const NAV_ITEMS: NavItem[] = [
  { path: "/overview", label: "Overview", icon: LayoutDashboard },
  { path: "/topology", label: "Topology", icon: Network },
  { path: "/runbooks", label: "Runbooks", icon: ScrollText },
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

  // 底部终端面板
  const [terminalCollapsed, setTerminalCollapsed] = useState(false);
  const toggleTerminal = useCallback(() => setTerminalCollapsed((v) => !v), []);
  const terminalRef = useRef<HTMLDivElement>(null);
  const focusTerminal = useCallback(() => {
    setTerminalCollapsed(false);
    // 等面板渲染后滚动到底部
    setTimeout(() => {
      terminalRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }, 60);
  }, []);

  const isTerminalRoute = pathname === "/terminal";
  const showGlobalTerminal = !isTerminalRoute;

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
          <Search className="absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-[var(--vigil-muted)]" />
          <input
            value={globalQuery}
            onChange={(e) => setGlobalQuery(e.target.value)}
            placeholder="Search hosts, services, commands…"
            className="vigil-input pl-9"
          />
        </form>

        <div className="ml-auto flex items-center gap-3 text-sm">
          {/* API Healthy */}
          <span
            className={cn("vigil-badge hidden lg:inline-flex", headerMeta.apiOk ? "text-[var(--vigil-ok)]" : "text-[var(--vigil-error)]")}
            title="GET /api/health"
          >
            <span className={cn("vigil-status-dot", headerMeta.apiOk ? "dot-ok" : "dot-error")} />
            API {headerMeta.apiOk ? "Healthy" : "Degraded"}
          </span>

          {/* Approvals N（当前 0） */}
          <button
            type="button"
            onClick={() => navigate("/approvals")}
            className="vigil-badge hidden lg:inline-flex hover:bg-[var(--vigil-muted-bg)]"
          >
            Approvals 0
          </button>

          {/* 运行时长 */}
          <span className="vigil-badge hidden lg:inline-flex">Agent upt: {headerMeta.uptime}</span>

          {/* 明暗切换 */}
          <button
            type="button"
            onClick={toggle}
            aria-label={dark ? "切换浅色模式" : "切换深色模式"}
            className="flex size-8 items-center justify-center rounded-md text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]"
          >
            {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
          </button>

          {/* 铃铛红点 → 审批中心 */}
          <button
            type="button"
            onClick={() => navigate("/approvals")}
            aria-label="Approvals"
            className="relative flex size-8 items-center justify-center rounded-md text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]"
          >
            <Bell className="size-4" />
            <span className="absolute right-1.5 top-1.5 size-1.5 rounded-full bg-red-500" />
          </button>

          {/* 设置菜单（主题 + 版本，无 Hermes 继承页入口） */}
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
            {/* Terminal 项：锚定底部终端面板 */}
            <button
              type="button"
              onClick={focusTerminal}
              title={collapsed ? "Terminal" : undefined}
              className={cn(
                "flex h-10 items-center rounded-md text-[var(--vigil-muted)] transition-colors hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)]",
                collapsed ? "w-10 justify-center" : "w-[176px] gap-2 px-2.5",
              )}
            >
              <Terminal className="size-[18px] shrink-0" />
              {!collapsed && <span className="truncate text-sm">Terminal</span>}
            </button>
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
          <div className={cn("min-h-0 flex-1 overflow-y-auto", showGlobalTerminal ? "p-4" : "")}>
            <Suspense
              fallback={
                <div className="flex min-h-[40vh] items-center justify-center text-sm text-[var(--vigil-muted)]">
                  加载中…
                </div>
              }
            >
              <Routes>
                <Route path="/" element={<Navigate to="/overview" replace />} />
                <Route path="/overview" element={<OverviewPage />} />
                <Route path="/topology" element={<TopologyPage />} />
                <Route path="/runbooks" element={<RunbooksPage />} />
                <Route path="/incidents" element={<IncidentsPage />} />
                <Route path="/approvals" element={<ApprovalsPage />} />
                <Route path="/audit" element={<AuditPage />} />
                <Route path="/terminal" element={<TerminalPage />} />
                <Route path="*" element={<Navigate to="/overview" replace />} />
              </Routes>
            </Suspense>
          </div>

          {/* 主体下区：全宽深色 Agent Terminal（Terminal 页自带大视图，不重复） */}
          {showGlobalTerminal && (
            <div ref={terminalRef} className="shrink-0 px-4 pb-4">
              <TerminalPanel collapsed={terminalCollapsed} onToggle={toggleTerminal} />
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

/** 顶部设置菜单：主题切换 + 版本信息（无 Hermes 继承页入口）。 */
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
