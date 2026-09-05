import { lazy, Suspense, useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router";
import { useTranslation } from "react-i18next";
import "@/i18n";
import { currentLang, switchLang } from "@/i18n";
import {
  Activity,
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
const MonitoringPage = lazy(() => import("@/pages/MonitoringPage"));

interface NavItem {
  path: string;
  /** i18n key (common.nav.*) resolved at render time. */
  label: string;
  icon: typeof LayoutDashboard;
}

/** Doubao menu, 9 items: 9 route pages. */
const NAV_ITEMS: NavItem[] = [
  { path: "/chat", label: "common.nav.chat", icon: MessageSquare },
  { path: "/overview", label: "common.nav.overview", icon: LayoutDashboard },
  { path: "/topology", label: "common.nav.topology", icon: Network },
  { path: "/runbooks", label: "common.nav.runbooks", icon: ScrollText },
  { path: "/matrix", label: "common.nav.matrix", icon: Grid3x3 },
  { path: "/incidents", label: "common.nav.incidents", icon: TriangleAlert },
  { path: "/approvals", label: "common.nav.approvals", icon: ShieldCheck },
  { path: "/audit", label: "common.nav.audit", icon: History },
  { path: "/monitoring", label: "common.nav.monitoring", icon: Activity },
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

/** zh | EN language switcher — flips i18n language, persisted to localStorage. */
function LangSwitch() {
  const { t } = useTranslation();
  const lang = currentLang();
  return (
    <button
      type="button"
      onClick={() => switchLang(lang === "zh" ? "en" : "zh")}
      aria-label={t("common.language")}
      title={t("common.language")}
      className="flex h-8 items-center justify-center rounded-md px-1.5 text-xs font-medium text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)]"
    >
      {lang === "zh" ? t("common.langEn") : t("common.langZh")}
    </button>
  );
}

export default function App() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { dark, toggle } = useConsoleTheme();

  // Header data: env label (first cluster) + API health + uptime
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
        const topoResp = topo as TopologyResponse | null;
        setHeaderMeta({
          apiOk: h?.ok !== false,
          uptime: formatUptime(h?.uptime_seconds),
          env:
            topoResp && topoResp.ok && topoResp.data && topoResp.data.clusters.length > 0
              ? topoResp.data.clusters[0].name
              : "",
        });
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  // Pending approval count (header badge + bell badge; batch 34: live via global polling)
  const approvalPoll = useApprovalPolling();

  // Sidebar: 60px icons-only by default, expandable to 200px
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

  // Global search (Enter jumps to topology with q)
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
      {/* Top header (h-14, Doubao layout) */}
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
              placeholder={t("common.searchPlaceholder")}
              className="h-full min-w-0 flex-1 bg-transparent text-sm text-[var(--vigil-text)] outline-none placeholder:text-[var(--vigil-muted)]/60"
            />
          </div>
        </form>

        <div className="ml-auto flex items-center gap-3 text-sm">
          {/* API healthy */}
          <span
            className={cn("vigil-badge hidden lg:inline-flex", headerMeta.apiOk ? "text-[var(--vigil-ok)]" : "text-[var(--vigil-error)]")}
            title={t("common.apiHealthTitle")}
          >
            <span className={cn("vigil-status-dot", headerMeta.apiOk ? "dot-ok" : "dot-error")} />
            API {headerMeta.apiOk ? "Healthy" : "Degraded"}
          </span>

          {/* Pending approvals (click → approval center) */}
          <button
            type="button"
            onClick={() => navigate("/approvals")}
            title={t("common.approvalsBadgeTitle")}
            className="vigil-badge hidden lg:inline-flex hover:bg-[var(--vigil-muted-bg)]"
          >
            Approvals {approvalPoll.total}
          </button>

          {/* Agent uptime */}
          <span className="vigil-badge hidden lg:inline-flex">Agent upt: {headerMeta.uptime}</span>

          {/* Light/dark toggle */}
          <button
            type="button"
            onClick={toggle}
            aria-label={dark ? t("common.themeToLight") : t("common.themeToDark")}
            title={dark ? t("common.themeToLight") : t("common.themeToDark")}
            className="flex size-8 items-center justify-center rounded-md text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]"
          >
            {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
          </button>

          {/* zh/EN language switch (persisted to localStorage) */}
          <LangSwitch />

          {/* Approval notifications (bell + count badge + dropdown, jumps to approval center) */}
          <ApprovalBell />

          {/* Settings menu (theme + version, no upstream-pages entry) */}
          <SettingsMenu dark={dark} onToggleTheme={toggle} />
        </div>
      </header>

      <div className="flex min-h-0 min-w-0 flex-1">
        {/* Left narrow sidebar (60px icons-only by default, expandable to 200px) */}
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
                  title={collapsed ? t(item.label) : undefined}
                  onClick={() => {
                    // Clicking the active nav item = back to overview (collapsed-state interaction)
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
                  {!collapsed && <span className="truncate text-sm">{t(item.label)}</span>}
                </NavLink>
              );
            })}
          </nav>

          <div className="flex justify-center pt-2">
            <button
              type="button"
              onClick={toggleCollapsed}
              aria-label={collapsed ? t("common.sidebarExpand") : t("common.sidebarCollapse")}
              className="flex size-8 items-center justify-center rounded-md text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]"
            >
              {collapsed ? <PanelLeftOpen className="size-4" /> : <PanelLeftClose className="size-4" />}
            </button>
          </div>
        </aside>

        {/* Main content */}
        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            <Suspense
              fallback={
                <div className="flex min-h-[40vh] items-center justify-center text-sm text-[var(--vigil-muted)]">
                  {t("common.loading")}
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
                <Route path="/monitoring" element={<MonitoringPage />} />
                <Route path="*" element={<Navigate to="/overview" replace />} />
              </Routes>
            </Suspense>
          </div>
        </main>
      </div>

      {/* Global approval modal (batch 34): visible on any route; decisions independent of chat-page approval cards */}
      <ApprovalModal />
    </div>
  );
}

/** Approval notifications: bell + count badge; the dropdown lists pending approvals and jumps to the approval center. */
function ApprovalBell() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  // Batch 34: bell badge/dropdown hooked to the global polling snapshot, reflecting pending approvals live.
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
        aria-label={t("common.bellAria")}
        title={t("common.bellTitle", { n: count })}
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
            {t("common.bellHeading", { n: count })}
          </div>
          {items.length === 0 ? (
            <div className="px-2.5 py-3 text-center text-xs text-[var(--vigil-muted)]">{t("common.bellEmpty")}</div>
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
              {t("common.bellOpen")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function SettingsMenu({ dark, onToggleTheme }: { dark: boolean; onToggleTheme: () => void }) {
  const { t } = useTranslation();
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
        aria-label={t("common.settingsAria")}
        aria-expanded={open}
        title={t("common.settingsTitle")}
        className="flex size-8 items-center justify-center rounded-md text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]"
      >
        <Settings className="size-4" />
      </button>
      {open && (
        <div className="absolute right-0 top-full z-50 mt-1 w-52 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)] p-1 shadow-[var(--vigil-shadow)]">
          <div className="px-2.5 py-1.5 text-[11px] font-medium tracking-wide text-[var(--vigil-muted)]">
            {t("common.settings")}
          </div>
          <MenuRow
            label={dark ? t("common.themeMenuDark") : t("common.themeMenuLight")}
            onClick={() => {
              onToggleTheme();
              setOpen(false);
            }}
          />
          <MenuRow label={t("common.versionLabel", { version })} onClick={() => setOpen(false)} />
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
