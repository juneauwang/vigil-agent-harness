import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";
import "@/i18n";
import { translateBackendMessage } from "@/lib/backendMsg";
import {
  ArrowRight,
  Building2,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Database,
  Grid3x3,
  HelpCircle,
  Info,
  Layers,
  ListChecks,
  Loader2,
  Lock,
  Pencil,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldAlert,
  ShieldCheck,
  TriangleAlert,
  User,
  UserCheck,
  Users,
  Wand2,
  X,
} from "lucide-react";
import {
  api,
  type MatrixData,
  type MatrixInitSelections,
  type MatrixLevel,
} from "@/lib/api";
import { cn, envClass } from "@/lib/ops";

/**
 * Ops matrix page (YAPL P3 §11.7 final) — permission control room.
 *
 * Rows = action enum, columns = environments, cells = three-state control
 * (execute / approve / {approve: required}) + per-cell source marker.
 *
 * The page is intentionally a dense "console": the matrix is a safety asset
 * and the operator should be able to scan risk, compare environments and
 * change a single cell without leaving the page. Every change goes through
 * PUT /api/matrix (the manual channel); the LLM only has read-only
 * matrix_query.
 */

type Level = MatrixLevel;
type LevelFilter = "all" | Level;
type FamilyId =
  | "read"
  | "lifecycle"
  | "release"
  | "data"
  | "config"
  | "host"
  | "package"
  | "execution"
  | "other";

interface Family {
  id: FamilyId;
  labelKey: string;
  actions: string[];
}

const FAMILIES: Family[] = [
  { id: "read", labelKey: "matrix.families.read", actions: ["query", "fetch_log", "verify"] },
  { id: "lifecycle", labelKey: "matrix.families.lifecycle", actions: ["start", "stop", "restart", "reload", "enable", "disable"] },
  { id: "release", labelKey: "matrix.families.release", actions: ["deploy", "rollback", "scale", "decommission"] },
  { id: "data", labelKey: "matrix.families.data", actions: ["backup", "restore", "transfer_file"] },
  { id: "config", labelKey: "matrix.families.config", actions: ["apply_config"] },
  { id: "host", labelKey: "matrix.families.host", actions: ["reboot", "shutdown"] },
  { id: "package", labelKey: "matrix.families.package", actions: ["install", "upgrade", "remove"] },
  { id: "execution", labelKey: "matrix.families.execution", actions: ["run_script", "runbook"] },
];

const OTHER_FAMILY: Family = { id: "other", labelKey: "matrix.families.other", actions: [] };
const ALL_FAMILIES = [...FAMILIES, OTHER_FAMILY];

const LEVELS: Level[] = ["execute", "approve", "required"];

const LEVEL_META: Record<
  Level,
  { icon: typeof Play; titleKey: string; shortKey: string; legendKey: string }
> = {
  execute: {
    icon: Play,
    titleKey: "matrix.levelTitle.execute",
    shortKey: "matrix.levelShort.execute",
    legendKey: "matrix.legendExecute",
  },
  approve: {
    icon: UserCheck,
    titleKey: "matrix.levelTitle.approve",
    shortKey: "matrix.levelShort.approve",
    legendKey: "matrix.legendApprove",
  },
  required: {
    icon: Lock,
    titleKey: "matrix.levelTitle.required",
    shortKey: "matrix.levelShort.required",
    legendKey: "matrix.legendRequired",
  },
};

const TEMPLATES: Array<{
  id: string;
  labelKey: string;
  descKey: string;
  icon: typeof User;
}> = [
  { id: "template1", labelKey: "matrix.templates.template1_label", descKey: "matrix.templates.template1_desc", icon: User },
  { id: "template2", labelKey: "matrix.templates.template2_label", descKey: "matrix.templates.template2_desc", icon: Users },
  { id: "template3", labelKey: "matrix.templates.template3_label", descKey: "matrix.templates.template3_desc", icon: Building2 },
  { id: "template4", labelKey: "matrix.templates.template4_label", descKey: "matrix.templates.template4_desc", icon: Wand2 },
];

const ENV_ORDER = ["local", "test", "uat", "dev", "prod", "unresolved"];
const ENV_RANK = new Map(ENV_ORDER.map((env, index) => [env, index]));

function envRank(env: string): number {
  return ENV_RANK.get(env) ?? ENV_ORDER.length + 1;
}

function familyOf(action: string): Family {
  return FAMILIES.find((family) => family.actions.includes(action)) ?? OTHER_FAMILY;
}

function errorText(value: unknown, fallback: string): string {
  if (typeof value === "string" && value.trim()) return value;
  if (value instanceof Error && value.message) return value.message;
  return fallback;
}

function formatMatrixTime(value?: string): string {
  if (!value) return "";
  return value.replace("T", " ").replace(/([+-]\d\d:\d\d|Z)$/, "").slice(0, 16);
}

function StatCard({
  tone,
  icon,
  label,
  value,
  sub,
}: {
  tone: "env" | "actions" | "required" | "manual" | "missing";
  icon: ReactNode;
  label: string;
  value: ReactNode;
  sub: string;
}) {
  return (
    <div className="vigil-card flex items-center gap-3 p-3.5">
      <div className="matrix-stat-icon" data-tone={tone}>
        {icon}
      </div>
      <div className="min-w-0">
        <div className="truncate text-[11px] text-[var(--vigil-muted)]">{label}</div>
        <div className="text-lg font-semibold leading-tight tabular-nums">{value}</div>
        <div className="truncate text-[10px] text-[var(--vigil-muted)] opacity-80">{sub}</div>
      </div>
    </div>
  );
}

function FilterChip({
  active,
  label,
  count,
  icon,
  onClick,
}: {
  active: boolean;
  label: string;
  count: number;
  icon: ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "inline-flex h-7 items-center gap-1.5 rounded-md border px-2 text-[11px] transition-colors",
        active
          ? "border-[var(--vigil-primary)]/40 bg-[var(--vigil-accent-bg)] text-[var(--vigil-accent-text)]"
          : "border-[var(--vigil-border)] text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)]",
      )}
    >
      {icon}
      <span>{label}</span>
      <span
        className={cn(
          "rounded px-1 font-mono text-[10px] tabular-nums",
          active ? "bg-[var(--vigil-card)]" : "bg-[var(--vigil-muted-bg)]",
        )}
      >
        {count}
      </span>
    </button>
  );
}

function MatrixSkeleton() {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4" aria-hidden="true">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        {Array.from({ length: 5 }).map((_, index) => (
          <div key={index} className="vigil-card flex items-center gap-3 p-3.5">
            <div className="vigil-skeleton size-[38px] rounded-[10px]" />
            <div className="min-w-0 flex-1 space-y-2">
              <div className="vigil-skeleton h-2.5 w-16" />
              <div className="vigil-skeleton h-5 w-10" />
              <div className="vigil-skeleton h-2 w-20" />
            </div>
          </div>
        ))}
      </div>
      <div className="vigil-card flex min-h-[320px] flex-1 flex-col p-4">
        <div className="vigil-skeleton h-8 w-64" />
        <div className="mt-4 space-y-2">
          {Array.from({ length: 9 }).map((_, index) => (
            <div key={index} className="vigil-skeleton h-9 w-full" />
          ))}
        </div>
      </div>
    </div>
  );
}

function SummaryBlock({
  level,
  label,
  actions,
  emptyLabel,
}: {
  level: Level;
  label: string;
  actions: string[];
  emptyLabel: string;
}) {
  const Icon = LEVEL_META[level].icon;
  return (
    <div className="matrix-review-block" data-level={level}>
      <div className="matrix-level-text flex items-center gap-1.5 text-[11px] font-medium" data-level={level}>
        <Icon className="size-3.5" />
        {label}
        <span className="ml-auto font-mono text-[10px] tabular-nums">{actions.length}</span>
      </div>
      <div className="scroll-thin mt-2 flex max-h-28 flex-wrap gap-1 overflow-y-auto">
        {actions.length > 0 ? (
          actions.map((action) => (
            <span
              key={action}
              className="rounded border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-1.5 py-0.5 font-mono text-[10px]"
            >
              {action}
            </span>
          ))
        ) : (
          <span className="text-[10px] text-[var(--vigil-muted)]">{emptyLabel}</span>
        )}
      </div>
    </div>
  );
}

export default function MatrixPage() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language === "en" ? "en" : "zh";

  const [data, setData] = useState<MatrixData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [initError, setInitError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<Record<string, boolean>>({});
  const [toast, setToast] = useState<string | null>(null);
  const [cascadeOpen, setCascadeOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [levelFilter, setLevelFilter] = useState<LevelFilter>("all");
  const [onlyManual, setOnlyManual] = useState(false);

  const reload = useCallback(() => {
    setLoading(true);
    setError(null);
    api
      .getMatrix()
      .then((resp) => {
        if (resp.ok && resp.data) setData(resp.data);
        else setError(resp.error ? String(resp.error) : t("matrix.loadFailed"));
      })
      .catch((e: unknown) => setError(errorText(e, t("matrix.loadFailed"))))
      .finally(() => setLoading(false));
  }, [t]);

  useEffect(() => {
    reload();
  }, [reload]);

  useEffect(() => {
    if (!toast) return;
    const id = window.setTimeout(() => setToast(null), 4200);
    return () => window.clearTimeout(id);
  }, [toast]);

  useEffect(() => {
    if (!cascadeOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setCascadeOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [cascadeOpen]);

  const envs = useMemo(() => {
    const keys = Object.keys(data?.matrix ?? {});
    return keys.sort((a, b) => envRank(a) - envRank(b) || a.localeCompare(b));
  }, [data]);

  // "unresolved" is the fallback row (always required); it must not inflate
  // the risk badges or the execute/approve/required KPI counts.
  const realEnvs = useMemo(
    () => envs.filter((env) => env !== "unresolved"),
    [envs],
  );

  const actions = useMemo(() => {
    const fromApi = data?.actions ?? [];
    if (fromApi.length > 0) return fromApi;
    const set = new Set<string>();
    Object.values(data?.matrix ?? {}).forEach((row) => {
      Object.keys(row).forEach((action) => set.add(action));
    });
    return Array.from(set);
  }, [data]);

  const stats = useMemo(() => {
    const requiredByEnv: Record<string, number> = {};
    const requiredByAction: Record<string, number> = {};
    let executeCells = 0;
    let approveCells = 0;
    let requiredCells = 0;
    let manualCells = 0;
    let missingCells = 0;

    for (const env of envs) {
      requiredByEnv[env] = 0;
      const realEnv = env !== "unresolved";
      for (const action of actions) {
        const raw = data?.matrix?.[env]?.[action] as Level | undefined;
        const level: Level = raw ?? "approve";
        if (!raw) missingCells += 1;
        if (level === "required") requiredByEnv[env] += 1;
        if (realEnv) {
          if (level === "execute") executeCells += 1;
          else if (level === "approve") approveCells += 1;
          else {
            requiredCells += 1;
            requiredByAction[action] = (requiredByAction[action] ?? 0) + 1;
          }
          if (data?.sources?.[env]?.[action] === "manual") manualCells += 1;
        }
      }
    }

    return {
      executeCells,
      approveCells,
      requiredCells,
      manualCells,
      missingCells,
      requiredByEnv,
      requiredByAction,
    };
  }, [actions, data, envs]);

  // Filter-chip counts are action counts (rows the filter will keep), not
  // cell counts — the KPI strip already reports cells.
  const filterCounts = useMemo(() => {
    const counts = { execute: 0, approve: 0, required: 0, manual: 0 };
    for (const action of actions) {
      if (realEnvs.some((env) => data?.sources?.[env]?.[action] === "manual")) {
        counts.manual += 1;
      }
      for (const level of LEVELS) {
        if (
          realEnvs.some(
            (env) =>
              ((data?.matrix?.[env]?.[action] as Level | undefined) ?? "approve") === level,
          )
        ) {
          counts[level] += 1;
        }
      }
    }
    return counts;
  }, [actions, data, realEnvs]);

  const visibleActions = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return actions.filter((action) => {
      if (needle) {
        const family = t(familyOf(action).labelKey).toLowerCase();
        if (!action.toLowerCase().includes(needle) && !family.includes(needle)) {
          return false;
        }
      }
      if (onlyManual && !realEnvs.some((env) => data?.sources?.[env]?.[action] === "manual")) {
        return false;
      }
      if (
        levelFilter !== "all" &&
        !envs.some(
          (env) =>
            env !== "unresolved" &&
            ((data?.matrix?.[env]?.[action] as Level | undefined) ?? "approve") === levelFilter,
        )
      ) {
        return false;
      }
      return true;
    });
  }, [actions, data, envs, levelFilter, onlyManual, query, realEnvs, t]);

  const groups = useMemo(() => {
    const byFamily = new Map<FamilyId, string[]>();
    for (const action of visibleActions) {
      const family = familyOf(action).id;
      const list = byFamily.get(family) ?? [];
      list.push(action);
      byFamily.set(family, list);
    }
    return ALL_FAMILIES.map((family) => ({
      family,
      actions: byFamily.get(family.id) ?? [],
    })).filter((group) => group.actions.length > 0);
  }, [visibleActions]);

  const isEmpty = data !== null && envs.length === 0;
  const hasFilters = query.trim().length > 0 || levelFilter !== "all" || onlyManual;

  const clearFilters = useCallback(() => {
    setQuery("");
    setLevelFilter("all");
    setOnlyManual(false);
  }, []);

  const setCellLocal = useCallback(
    (env: string, action: string, level: Level | null, source: string | null) => {
      setData((prev) => {
        if (!prev) return prev;
        const matrix = { ...prev.matrix, [env]: { ...(prev.matrix[env] ?? {}) } };
        const sources = { ...prev.sources, [env]: { ...(prev.sources[env] ?? {}) } };
        if (level) matrix[env][action] = level;
        else delete matrix[env][action];
        if (source) sources[env][action] = source;
        else delete sources[env][action];
        return {
          ...prev,
          matrix,
          sources,
          source: level ? "manual" : prev.source,
        };
      });
    },
    [],
  );

  const changeCell = (env: string, action: string, level: Level) => {
    const current = (data?.matrix?.[env]?.[action] as Level | undefined) ?? null;
    if (current === level) return;
    const previousSource = data?.sources?.[env]?.[action] ?? null;
    const cellKey = `${env}:${action}`;

    setCellLocal(env, action, level, "manual");
    setSaving((prev) => ({ ...prev, [cellKey]: true }));
    api
      .setMatrixCell(env, action, level)
      .then((resp) => {
        if (resp.ok) {
          setError(null);
          setToast(t("matrix.updatedMsg", { env, action, level }));
        } else {
          setCellLocal(env, action, current, previousSource);
          setError(resp.error ? String(resp.error) : t("matrix.updateFailed"));
        }
      })
      .catch((e: unknown) => {
        setCellLocal(env, action, current, previousSource);
        setError(errorText(e, t("matrix.updateFailed")));
      })
      .finally(() => {
        setSaving((prev) => {
          const next = { ...prev };
          delete next[cellKey];
          return next;
        });
      });
  };

  const initTemplate = (template: string, selections?: MatrixInitSelections) => {
    setInitError(null);
    api
      .initMatrix(template, selections)
      .then((resp) => {
        if (resp.ok && resp.data) {
          setData(resp.data);
          setToast(t("matrix.generatedMsg", { template }));
        } else {
          setInitError(resp.error ? String(resp.error) : t("matrix.initFailed"));
        }
      })
      .catch((e: unknown) => setInitError(errorText(e, t("matrix.initFailed"))));
  };

  const warnings = data?.warnings ?? [];

  return (
    <div className="flex h-full min-h-0 flex-col gap-4">
      {/* Hero ---------------------------------------------------------- */}
      <section className="matrix-hero vigil-card relative shrink-0 overflow-hidden p-5">
        <div className="relative flex flex-wrap items-start gap-4">
          <div className="flex size-11 shrink-0 items-center justify-center rounded-xl bg-[var(--vigil-accent-bg)] text-[var(--vigil-accent-text)]">
            <Grid3x3 className="size-5" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-[10px] font-semibold uppercase tracking-[0.2em] text-[var(--vigil-muted)]">
              {t("matrix.eyebrow")}
            </div>
            <h1 className="mt-1 text-xl font-semibold tracking-tight">{t("matrix.title")}</h1>
            <p className="mt-1 max-w-3xl text-xs leading-relaxed text-[var(--vigil-muted)]">
              {t("matrix.headerNote")}
            </p>
          </div>
          <div className="flex w-full max-w-full flex-wrap items-center gap-2 sm:ml-auto sm:w-auto sm:justify-end">
            <span className="vigil-badge border-[var(--matrix-execute-border)] bg-[var(--matrix-execute-soft)] text-[var(--matrix-execute)]">
              <ShieldCheck className="size-3.5" />
              {t("matrix.safetyBadge")}
            </span>
            {data?.updated_at && (
              <span className="vigil-badge" title={data.updated_at}>
                <Clock className="size-3.5" />
                {t("matrix.updatedAt", { time: formatMatrixTime(data.updated_at) })}
              </span>
            )}
            {data?.source && (
              <span className="vigil-badge">
                <Database className="size-3.5" />
                {t("matrix.sourceMeta", { source: data.source })}
              </span>
            )}
            {data?.base_template && (
              <span className="vigil-badge hidden xl:inline-flex">
                {t("matrix.baseTemplate", { tpl: data.base_template })}
              </span>
            )}
          </div>
        </div>
      </section>

      {loading && !data && <MatrixSkeleton />}

      {!loading && !data && error && (
        <div className="vigil-card flex flex-1 flex-col items-center justify-center gap-3 p-10 text-center">
          <TriangleAlert className="size-8 text-[var(--matrix-required)]" />
          <div className="text-sm text-[var(--vigil-text)]">
            {translateBackendMessage(error, lang)}
          </div>
          <button type="button" className="vigil-btn vigil-btn-primary" onClick={reload}>
            <RefreshCw className="size-3.5" />
            {t("matrix.retry")}
          </button>
        </div>
      )}

      {data && (
        <>
          {/* KPI strip ------------------------------------------------- */}
          <div className="grid shrink-0 grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
            <StatCard
              tone="env"
              icon={<Layers className="size-4" />}
              label={t("matrix.statEnvironments")}
              value={envs.length}
              sub={t("matrix.statEnvironmentsSub")}
            />
            <StatCard
              tone="actions"
              icon={<ListChecks className="size-4" />}
              label={t("matrix.statActions")}
              value={actions.length}
              sub={t("matrix.statActionsSub")}
            />
            <StatCard
              tone="required"
              icon={<ShieldAlert className="size-4" />}
              label={t("matrix.statRequired")}
              value={stats.requiredCells}
              sub={t("matrix.statRequiredSub")}
            />
            <StatCard
              tone="manual"
              icon={<Pencil className="size-4" />}
              label={t("matrix.statManual")}
              value={stats.manualCells}
              sub={t("matrix.statManualSub")}
            />
            <StatCard
              tone="missing"
              icon={<HelpCircle className="size-4" />}
              label={t("matrix.statMissing")}
              value={stats.missingCells}
              sub={t("matrix.statMissingSub")}
            />
          </div>

          {error && (
            <div className="flex items-center gap-2 rounded-md border border-[var(--vigil-error)]/40 bg-[var(--vigil-error)]/10 px-3 py-2 text-xs text-[var(--vigil-error)]">
              <TriangleAlert className="size-4 shrink-0" />
              <span className="min-w-0 flex-1">{translateBackendMessage(error, lang)}</span>
              <button type="button" className="vigil-link shrink-0 text-xs" onClick={reload}>
                {t("matrix.retry")}
              </button>
              <button
                type="button"
                aria-label={t("common.close")}
                className="shrink-0 rounded p-0.5 hover:bg-[var(--vigil-error)]/10"
                onClick={() => setError(null)}
              >
                <X className="size-3.5" />
              </button>
            </div>
          )}

          {isEmpty ? (
            <div className="vigil-card flex min-h-0 flex-1 flex-col overflow-y-auto p-5">
              <div className="mx-auto my-auto w-full max-w-5xl">
                <div className="flex flex-wrap items-start gap-3">
                  <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-[var(--vigil-accent-bg)] text-[var(--vigil-accent-text)]">
                    <ShieldCheck className="size-5" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <h2 className="text-sm font-semibold">{t("matrix.emptyTitle")}</h2>
                    <p className="mt-1 max-w-2xl text-xs leading-relaxed text-[var(--vigil-muted)]">
                      {t("matrix.emptyDesc")}
                    </p>
                  </div>
                </div>

                <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                  {TEMPLATES.map((tpl) => {
                    const Icon = tpl.icon;
                    return (
                      <button
                        key={tpl.id}
                        type="button"
                        onClick={() => (tpl.id === "template4" ? setCascadeOpen(true) : initTemplate(tpl.id))}
                        className="group vigil-card vigil-card-interactive relative overflow-hidden p-4 text-left hover:border-[var(--vigil-primary)]/60"
                      >
                        <div className="flex items-start gap-3">
                          <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-[var(--vigil-muted-bg)] text-[var(--vigil-muted)] group-hover:bg-[var(--vigil-accent-bg)] group-hover:text-[var(--vigil-accent-text)]">
                            <Icon className="size-4" />
                          </div>
                          <div className="min-w-0">
                            <div className="text-xs font-medium">{t(tpl.labelKey)}</div>
                            <div className="mt-1 text-[10px] leading-relaxed text-[var(--vigil-muted)]">
                              {t(tpl.descKey)}
                            </div>
                          </div>
                        </div>
                        <div className="mt-4 flex items-center gap-1">
                          <span className="h-1.5 w-9 rounded-full bg-[var(--matrix-execute)]" />
                          <span className="h-1.5 w-6 rounded-full bg-[var(--matrix-approve)]" />
                          <span className="h-1.5 w-3 rounded-full bg-[var(--matrix-required)]" />
                          <ArrowRight className="ml-auto size-3.5 text-[var(--vigil-muted)] transition-transform group-hover:translate-x-0.5 group-hover:text-[var(--vigil-primary)]" />
                        </div>
                      </button>
                    );
                  })}
                </div>

                <div className="mt-4 flex items-start gap-2 text-[10px] text-[var(--vigil-muted)]">
                  <Info className="mt-0.5 size-3.5 shrink-0" />
                  <span>{t("matrix.emptyHint")}</span>
                </div>

                {initError && (
                  <div className="mt-4 flex items-center gap-2 rounded-md border border-[var(--vigil-error)]/40 bg-[var(--vigil-error)]/10 px-3 py-2 text-xs text-[var(--vigil-error)]">
                    <TriangleAlert className="size-4 shrink-0" />
                    <span className="min-w-0 flex-1">{translateBackendMessage(initError, lang)}</span>
                  </div>
                )}
              </div>
            </div>
          ) : (
            <>
              {/* Toolbar ------------------------------------------------ */}
              <div className="vigil-card flex flex-wrap items-center gap-2 px-3 py-2.5">
                <div className="flex min-w-[220px] flex-1 items-center gap-2">
                  <div className="relative min-w-0 flex-1">
                    <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-[var(--vigil-muted)]" />
                    <input
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder={t("matrix.searchPlaceholder")}
                      aria-label={t("matrix.searchPlaceholder")}
                      className="vigil-input vigil-input-icon h-8 text-xs"
                    />
                  </div>
                  <button
                    type="button"
                    onClick={reload}
                    disabled={loading}
                    title={t("matrix.refresh")}
                    aria-label={t("matrix.refresh")}
                    className="flex size-8 shrink-0 items-center justify-center rounded-md text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)] disabled:opacity-50"
                  >
                    <RefreshCw className={cn("size-3.5", loading && "animate-spin")} />
                  </button>
                </div>

                <div className="flex flex-wrap items-center gap-1">
                  <FilterChip
                    active={levelFilter === "all"}
                    label={t("matrix.filterAll")}
                    count={actions.length}
                    icon={<ListChecks className="size-3" />}
                    onClick={() => setLevelFilter("all")}
                  />
                  {LEVELS.map((level) => {
                    const Icon = LEVEL_META[level].icon;
                    return (
                      <FilterChip
                        key={level}
                        active={levelFilter === level}
                        label={t(LEVEL_META[level].shortKey)}
                        count={filterCounts[level]}
                        icon={<Icon className="size-3" />}
                        onClick={() => setLevelFilter(levelFilter === level ? "all" : level)}
                      />
                    );
                  })}
                </div>

                <button
                  type="button"
                  aria-pressed={onlyManual}
                  onClick={() => setOnlyManual((value) => !value)}
                  className={cn(
                    "inline-flex h-7 items-center gap-1.5 rounded-md border px-2 text-[11px] transition-colors",
                    onlyManual
                      ? "border-[var(--matrix-manual)]/40 bg-[var(--matrix-manual-soft)] text-[var(--matrix-manual)]"
                      : "border-[var(--vigil-border)] text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]",
                  )}
                >
                  <Pencil className="size-3" />
                  {t("matrix.onlyManual")}
                  <span className="rounded bg-[var(--vigil-card)] px-1 font-mono text-[10px] tabular-nums">
                    {filterCounts.manual}
                  </span>
                </button>

                {hasFilters && (
                  <button
                    type="button"
                    onClick={clearFilters}
                    className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-[11px] text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)]"
                  >
                    <RotateCcw className="size-3" />
                    {t("matrix.clearFilters")}
                  </button>
                )}

              </div>

              {warnings.length > 0 && (
                <details className="vigil-card group shrink-0 border-[var(--vigil-warn)]/30 bg-[var(--vigil-warn)]/5 px-3 py-2">
                  <summary className="matrix-warning-summary flex items-center gap-2 text-xs font-medium text-[var(--vigil-warn)]">
                    <TriangleAlert className="size-4 shrink-0" />
                    <span>{t("matrix.warningsTitle")}</span>
                    <span className="rounded bg-[var(--vigil-warn)]/15 px-1.5 py-0.5 font-mono text-[10px]">
                      {warnings.length}
                    </span>
                    <ChevronDown className="ml-auto size-3.5 transition-transform group-open:rotate-180" />
                  </summary>
                  <ul className="mt-2 space-y-1 pl-6 text-[11px] leading-relaxed text-[var(--vigil-text-secondary)]">
                    {warnings.map((warning, index) => (
                      <li key={index} className="list-disc">
                        {translateBackendMessage(warning, lang)}
                      </li>
                    ))}
                  </ul>
                </details>
              )}

              {/* Matrix table ------------------------------------------- */}
              <div className="vigil-card flex min-h-[320px] flex-1 flex-col overflow-hidden">
                <div className="flex flex-wrap items-end gap-3 border-b border-[var(--vigil-border)] px-4 py-3">
                  <div className="min-w-0">
                    <h2 className="text-sm font-semibold">{t("matrix.tableTitle")}</h2>
                    <p className="mt-0.5 text-[10px] text-[var(--vigil-muted)]">
                      {t("matrix.tableSubtitle")}
                    </p>
                  </div>
                  <div className="ml-auto flex flex-wrap items-center gap-3 text-[10px] text-[var(--vigil-muted)]">
                    {LEVELS.map((level) => {
                      const Icon = LEVEL_META[level].icon;
                      return (
                        <span key={level} className="inline-flex items-center gap-1">
                          <Icon className="size-3" />
                          <span className="matrix-level-text" data-level={level}>
                            {t(LEVEL_META[level].legendKey)}
                          </span>
                        </span>
                      );
                    })}
                    <span className="inline-flex items-center gap-1">
                      <span className="matrix-source-dot" data-source="manual" />
                      {t("matrix.legendManual")}
                    </span>
                    <span className="inline-flex items-center gap-1">
                      <span className="matrix-source-dot" data-source="missing" />
                      {t("matrix.legendMissing")}
                    </span>
                  </div>
                </div>

                {visibleActions.length === 0 ? (
                  <div className="flex flex-1 flex-col items-center justify-center gap-2 p-10 text-center">
                    <Search className="size-7 text-[var(--vigil-muted)] opacity-50" />
                    <div className="text-sm font-medium text-[var(--vigil-text)] opacity-80">
                      {t("matrix.noMatchTitle")}
                    </div>
                    <div className="text-xs text-[var(--vigil-muted)]">{t("matrix.noMatchHint")}</div>
                    <button type="button" className="vigil-btn" onClick={clearFilters}>
                      <RotateCcw className="size-3.5" />
                      {t("matrix.clearFilters")}
                    </button>
                  </div>
                ) : (
                  <div className="scroll-thin min-h-0 flex-1 overflow-auto">
                    <table className="matrix-table min-w-[760px] text-xs">
                      <caption className="sr-only">
                        {t("matrix.tableTitle")} — {t("matrix.headerNote")}
                      </caption>
                      <thead>
                        <tr>
                          <th className="matrix-action-col w-[176px] min-w-[176px] px-4 py-2.5 text-left align-bottom">
                            <div className="flex items-center gap-1.5 text-[11px] font-medium text-[var(--vigil-muted)]">
                              <ListChecks className="size-3.5" />
                              {t("matrix.thAction")}
                            </div>
                            <div className="mt-0.5 text-[10px] font-normal text-[var(--vigil-muted)] opacity-70">
                              {t("matrix.actionsCount", { n: visibleActions.length })}
                            </div>
                          </th>
                          {envs.map((env) => (
                            <th key={env} className="min-w-[112px] px-2 py-2.5 text-left align-bottom">
                              <div className="flex items-center gap-1.5">
                                <span className={cn("vigil-env", envClass(env))}>{env}</span>
                                {env === "unresolved" && (
                                  <span title={t("matrix.unresolvedHint")}>
                                    <HelpCircle className="size-3 text-[var(--vigil-muted)]" aria-hidden="true" />
                                  </span>
                                )}
                              </div>
                              <div className="mt-1 text-[10px] font-normal text-[var(--vigil-muted)]">
                                {t("matrix.envRequired", { n: stats.requiredByEnv[env] ?? 0 })}
                              </div>
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {groups.map((group) => (
                          <Fragment key={group.family.id}>
                            <tr className="matrix-group-row">
                              <td colSpan={envs.length + 1} className="px-4 py-1.5">
                                <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-[0.12em] text-[var(--vigil-muted)]">
                                  <Layers className="size-3" />
                                  {t(group.family.labelKey)}
                                  <span className="font-mono tracking-normal opacity-70">
                                    {group.actions.length}
                                  </span>
                                </div>
                              </td>
                            </tr>
                            {group.actions.map((action) => {
                              const requiredCount = stats.requiredByAction[action] ?? 0;
                              const allExecute =
                                realEnvs.length > 0 &&
                                realEnvs.every(
                                  (env) =>
                                    ((data?.matrix?.[env]?.[action] as Level | undefined) ?? "approve") ===
                                    "execute",
                                );
                              const risk = requiredCount > 0 ? "required" : allExecute ? "execute" : "approve";
                              const riskTitle =
                                requiredCount > 0
                                  ? t("matrix.actionRisk", { n: requiredCount })
                                  : allExecute
                                    ? t("matrix.actionAllExecute")
                                    : t("matrix.actionMixed");
                              return (
                                <tr key={action} data-row-action={action}>
                                  <td className="matrix-action-col px-4 py-1.5">
                                    <div className="flex items-center justify-between gap-2">
                                      <span className="truncate font-mono text-[11px]" title={action}>
                                        {action}
                                      </span>
                                      <span
                                        className="matrix-action-risk"
                                        data-risk={risk}
                                        title={riskTitle}
                                        role="img"
                                        aria-label={riskTitle}
                                      >
                                        {risk === "required" ? (
                                          <ShieldAlert className="size-3.5" />
                                        ) : risk === "execute" ? (
                                          <CheckCircle2 className="size-3.5" />
                                        ) : (
                                          <UserCheck className="size-3.5" />
                                        )}
                                      </span>
                                    </div>
                                  </td>
                                  {envs.map((env) => {
                                    const raw = data?.matrix?.[env]?.[action] as Level | undefined;
                                    const level: Level = raw ?? "approve";
                                    const configured = Boolean(raw);
                                    const source = data?.sources?.[env]?.[action] ?? "";
                                    const sourceKind =
                                      source === "manual"
                                        ? "manual"
                                        : source || configured
                                          ? "template"
                                          : "missing";
                                    const sourceTitle =
                                      sourceKind === "manual"
                                        ? t("matrix.sourceManual")
                                        : sourceKind === "template"
                                          ? source
                                            ? t("matrix.sourceTemplate", { source })
                                            : t("matrix.sourceUnknown")
                                          : t("matrix.sourceMissing");
                                    const busy = Boolean(saving[`${env}:${action}`]);
                                    return (
                                      <td
                                        key={env}
                                        data-action={action}
                                        data-env={env}
                                        className="px-2 py-1.5"
                                      >
                                        <div className="flex items-center gap-1.5">
                                          <div
                                            className="matrix-seg-group"
                                            data-configured={configured}
                                            data-saving={busy}
                                            role="group"
                                            aria-label={t("matrix.cellAria", {
                                              action,
                                              env,
                                              level: t(LEVEL_META[level].titleKey),
                                            })}
                                          >
                                            {LEVELS.map((option) => {
                                              const Icon = LEVEL_META[option].icon;
                                              const selected = option === level;
                                              return (
                                                <button
                                                  key={option}
                                                  type="button"
                                                  data-level={option}
                                                  data-selected={selected}
                                                  aria-pressed={selected}
                                                  aria-label={t("matrix.cellAria", {
                                                    action,
                                                    env,
                                                    level: t(LEVEL_META[option].titleKey),
                                                  })}
                                                  title={t(LEVEL_META[option].titleKey)}
                                                  disabled={busy}
                                                  onClick={() => changeCell(env, action, option)}
                                                  className="matrix-seg"
                                                >
                                                  <Icon className="size-3.5" />
                                                </button>
                                              );
                                            })}
                                          </div>
                                          <span
                                            className="matrix-source-dot"
                                            data-source={sourceKind}
                                            role="img"
                                            aria-label={sourceTitle}
                                            title={sourceTitle}
                                          />
                                          {busy && (
                                            <Loader2
                                              className="size-3 animate-spin text-[var(--vigil-muted)]"
                                              aria-label={t("matrix.cellSaving")}
                                            />
                                          )}
                                          {!configured && <span className="sr-only">{t("matrix.cellMissing")}</span>}
                                        </div>
                                      </td>
                                    );
                                  })}
                                </tr>
                              );
                            })}
                          </Fragment>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}

                <div className="flex shrink-0 items-start gap-1.5 border-t border-[var(--vigil-border)] px-4 py-2 text-[10px] leading-relaxed text-[var(--vigil-muted)]">
                  <Info className="mt-0.5 size-3.5 shrink-0" />
                  <span>{t("matrix.footnote")}</span>
                </div>
              </div>
            </>
          )}
        </>
      )}

      {/* Toast --------------------------------------------------------- */}
      {toast && (
        <div
          role="status"
          aria-live="polite"
          className="matrix-toast fixed bottom-5 right-5 z-[60] flex max-w-sm items-start gap-2 rounded-lg border border-[var(--matrix-execute-border)] bg-[var(--vigil-card)] p-3 shadow-lg"
        >
          <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-[var(--matrix-execute)]" />
          <div className="min-w-0 flex-1 text-xs leading-relaxed text-[var(--vigil-text)]">
            {toast}
          </div>
          <button
            type="button"
            onClick={() => setToast(null)}
            aria-label={t("common.close")}
            className="shrink-0 rounded p-0.5 text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)]"
          >
            <X className="size-3.5" />
          </button>
        </div>
      )}

      {/* Template 4 cascade -------------------------------------------- */}
      {cascadeOpen && (
        <CascadeModal
          actions={actions}
          onCancel={() => setCascadeOpen(false)}
          onConfirm={(selections) => {
            setCascadeOpen(false);
            initTemplate("template4", selections);
          }}
        />
      )}
    </div>
  );
}

/**
 * Template 4 cascade (§11.3): execute set → approve set → review.
 * The review step is the third step of the "three-step cascade"; the rest of
 * the actions automatically become {approve: required}.
 */
function CascadeModal({
  actions,
  onCancel,
  onConfirm,
}: {
  actions: string[];
  onCancel: () => void;
  onConfirm: (selections: MatrixInitSelections) => void;
}) {
  const { t } = useTranslation();
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [executeSet, setExecuteSet] = useState<string[]>([]);
  const [approveSet, setApproveSet] = useState<string[]>([]);

  const remaining = actions.filter((action) => !executeSet.includes(action));
  const effectiveApprove = approveSet.filter((action) => remaining.includes(action));
  const requiredSet = remaining.filter((action) => !effectiveApprove.includes(action));

  const toggle = (list: string[], setList: (value: string[]) => void, action: string) => {
    setList(list.includes(action) ? list.filter((item) => item !== action) : [...list, action]);
  };

  const stepLabel = t("matrix.cascadeStepLabel", { n: step });
  const stageItems = [
    { n: 1, labelKey: "matrix.levelShort.execute" },
    { n: 2, labelKey: "matrix.levelShort.approve" },
    { n: 3, labelKey: "matrix.levelShort.required" },
  ];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-4 backdrop-blur-[2px]"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onCancel();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="matrix-cascade-title"
        className="matrix-modal flex max-h-[88vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-[var(--vigil-border)] bg-[var(--vigil-card)] shadow-2xl"
      >
        <div className="flex items-start gap-3 border-b border-[var(--vigil-border)] px-5 py-4">
          <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-[var(--vigil-accent-bg)] text-[var(--vigil-accent-text)]">
            <Wand2 className="size-4" />
          </div>
          <div className="min-w-0 flex-1">
            <h2 id="matrix-cascade-title" className="text-sm font-semibold">
              {t("matrix.cascadeTitle", { step: stepLabel })}
            </h2>
            <p className="mt-0.5 text-[10px] text-[var(--vigil-muted)]">
              {t("matrix.cascadeProgress", { step, total: 3 })}
            </p>
          </div>
          <button
            type="button"
            onClick={onCancel}
            aria-label={t("common.close")}
            autoFocus
            className="rounded-md p-1 text-[var(--vigil-muted)] outline-none hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)] focus-visible:ring-2 focus-visible:ring-[var(--vigil-primary)] focus-visible:ring-offset-1 focus-visible:ring-offset-[var(--vigil-card)]"
          >
            <X className="size-4" />
          </button>
        </div>

        <div className="border-b border-[var(--vigil-border)] px-5 py-3">
          <div className="flex items-center gap-1.5">
            {stageItems.map((item) => (
              <Fragment key={item.n}>
                <div
                  className={cn(
                    "flex h-6 w-6 items-center justify-center rounded-full border text-[10px] font-mono tabular-nums",
                    step === item.n
                      ? "border-[var(--vigil-primary)] bg-[var(--vigil-primary)] text-white"
                      : step > item.n
                        ? "border-[var(--matrix-execute-border)] bg-[var(--matrix-execute-soft)] text-[var(--matrix-execute)]"
                        : "border-[var(--vigil-border)] text-[var(--vigil-muted)]",
                  )}
                  aria-current={step === item.n ? "step" : undefined}
                >
                  {step > item.n ? <CheckCircle2 className="size-3" /> : item.n}
                </div>
                <span
                  className={cn(
                    "text-[10px]",
                    step === item.n ? "font-medium text-[var(--vigil-text)]" : "text-[var(--vigil-muted)]",
                  )}
                >
                  {t(item.labelKey)}
                </span>
                {item.n < 3 && <ChevronRight className="size-3 text-[var(--vigil-muted)] opacity-50" />}
              </Fragment>
            ))}
          </div>
          <div className="mt-2 flex gap-1">
            {[1, 2, 3].map((n) => (
              <div
                key={n}
                className={cn(
                  "h-1 flex-1 rounded-full",
                  step >= n ? "bg-[var(--vigil-primary)]" : "bg-[var(--vigil-muted-bg)]",
                )}
              />
            ))}
          </div>
        </div>

        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {step === 1 && (
            <>
              <p className="mb-3 text-xs leading-relaxed text-[var(--vigil-muted)]">
                {t("matrix.cascadeHint1")}
              </p>
              <div className="mb-2 flex items-center gap-2 text-xs font-medium">
                {t("matrix.executeSet")}
                <span className="text-[var(--vigil-muted)]">
                  {t("matrix.selectedCount", { n: executeSet.length })}
                </span>
                <button
                  type="button"
                  className="ml-auto rounded border border-[var(--vigil-border)] px-2 py-0.5 text-[10px] hover:border-[var(--vigil-primary)]"
                  onClick={() => setExecuteSet(executeSet.length === actions.length ? [] : [...actions])}
                >
                  {executeSet.length === actions.length ? t("matrix.clearAll") : t("matrix.selectAll")}
                </button>
              </div>
              <div className="grid grid-cols-2 gap-1.5">
                {actions.map((action) => {
                  const checked = executeSet.includes(action);
                  return (
                    <label
                      key={action}
                      className={cn(
                        "flex cursor-pointer items-center gap-2 rounded-md border px-2 py-1.5 text-xs transition-colors",
                        checked
                          ? "border-[var(--matrix-execute-border)] bg-[var(--matrix-execute-soft)]"
                          : "border-[var(--vigil-border)] hover:border-[var(--vigil-primary)]",
                      )}
                    >
                      <input
                        type="checkbox"
                        className="size-3.5 accent-[var(--vigil-primary)]"
                        checked={checked}
                        onChange={() => toggle(executeSet, setExecuteSet, action)}
                      />
                      <span className="font-mono text-[11px]">{action}</span>
                    </label>
                  );
                })}
              </div>
            </>
          )}

          {step === 2 && (
            <>
              <p className="mb-3 text-xs leading-relaxed text-[var(--vigil-muted)]">
                {t("matrix.cascadeHint2")}
              </p>
              <div className="mb-2 flex items-center gap-2 text-xs font-medium">
                {t("matrix.approveSet")}
                <span className="text-[var(--vigil-muted)]">
                  {t("matrix.selectedCount", { n: effectiveApprove.length })}
                </span>
                <button
                  type="button"
                  className="ml-auto rounded border border-[var(--vigil-border)] px-2 py-0.5 text-[10px] hover:border-[var(--vigil-primary)]"
                  onClick={() =>
                    setApproveSet(
                      effectiveApprove.length === remaining.length ? [] : [...remaining],
                    )
                  }
                >
                  {effectiveApprove.length === remaining.length
                    ? t("matrix.clearAll")
                    : t("matrix.selectAll")}
                </button>
              </div>
              {remaining.length > 0 ? (
                <div className="grid grid-cols-2 gap-1.5">
                  {remaining.map((action) => {
                    const checked = effectiveApprove.includes(action);
                    return (
                      <label
                        key={action}
                        className={cn(
                          "flex cursor-pointer items-center gap-2 rounded-md border px-2 py-1.5 text-xs transition-colors",
                          checked
                            ? "border-[var(--matrix-approve-border)] bg-[var(--matrix-approve-soft)]"
                            : "border-[var(--vigil-border)] hover:border-[var(--vigil-primary)]",
                        )}
                      >
                        <input
                          type="checkbox"
                          className="size-3.5 accent-[var(--vigil-primary)]"
                          checked={checked}
                          onChange={() => toggle(effectiveApprove, setApproveSet, action)}
                        />
                        <span className="font-mono text-[11px]">{action}</span>
                      </label>
                    );
                  })}
                </div>
              ) : (
                <div className="rounded-md border border-dashed border-[var(--vigil-border)] px-3 py-6 text-center text-xs text-[var(--vigil-muted)]">
                  {t("matrix.cascadeNoRemaining")}
                </div>
              )}
              <div className="mt-3 rounded-md border border-[var(--matrix-required-border)] bg-[var(--matrix-required-soft)] px-3 py-2 text-xs">
                <span className="matrix-level-text font-medium" data-level="required">
                  {t("matrix.requiredLabel")}
                </span>
                <span className="ml-1 text-[var(--vigil-muted)]">
                  {t("matrix.requiredCount", {
                    n: requiredSet.length,
                    list: requiredSet.join(", ") || t("matrix.requiredNone"),
                  })}
                </span>
              </div>
            </>
          )}

          {step === 3 && (
            <>
              <div className="mb-3">
                <h3 className="text-xs font-medium">{t("matrix.cascadeReviewTitle")}</h3>
                <p className="mt-0.5 text-[10px] text-[var(--vigil-muted)]">
                  {t("matrix.cascadeReviewHint")}
                </p>
              </div>
              <div className="grid gap-2 sm:grid-cols-3">
                <SummaryBlock
                  level="execute"
                  label={t("matrix.cascadeSummaryExecute")}
                  actions={executeSet}
                  emptyLabel={t("matrix.cascadeEmpty")}
                />
                <SummaryBlock
                  level="approve"
                  label={t("matrix.cascadeSummaryApprove")}
                  actions={effectiveApprove}
                  emptyLabel={t("matrix.cascadeEmpty")}
                />
                <SummaryBlock
                  level="required"
                  label={t("matrix.cascadeSummaryRequired")}
                  actions={requiredSet}
                  emptyLabel={t("matrix.cascadeEmpty")}
                />
              </div>
              <div className="mt-3 flex items-start gap-1.5 text-[10px] text-[var(--vigil-muted)]">
                <Info className="mt-0.5 size-3.5 shrink-0" />
                {t("matrix.cascadeGenerateHint")}
              </div>
            </>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 border-t border-[var(--vigil-border)] px-5 py-3">
          <button type="button" className="vigil-btn" onClick={onCancel}>
            {t("common.cancel")}
          </button>
          <div className="flex items-center gap-2">
            {step > 1 && (
              <button
                type="button"
                className="vigil-btn"
                onClick={() => setStep((step - 1) as 1 | 2 | 3)}
              >
                {t("matrix.prevStep")}
              </button>
            )}
            {step < 3 ? (
              <button
                type="button"
                className="vigil-btn vigil-btn-primary"
                onClick={() => setStep((step + 1) as 1 | 2 | 3)}
              >
                {t("matrix.nextStep")}
              </button>
            ) : (
              <button
                type="button"
                className="vigil-btn vigil-btn-primary"
                onClick={() => onConfirm({ execute: executeSet, approve: effectiveApprove })}
              >
                {t("matrix.generate")}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
