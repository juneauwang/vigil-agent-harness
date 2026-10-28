import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import "@/i18n";
import { CalendarClock, Check, Loader2, Pause, Play, Plus, RefreshCw, Timer, Trash2, X, Zap } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import type { CronBlueprint, CronJob } from "@/lib/api";
import { translateBackendMessage } from "@/lib/backendMsg";
import { currentLang } from "@/i18n";
import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { cn } from "@/lib/ops";
import { windowArg, withWindowArg } from "@/lib/schedules";

/**
 * /schedules（D2）：所有 cron 作业一张表——看得见（下次运行 / 最近状态）、
 * 改得动（计划 + digest 窗口）、按得下（暂停/恢复/立即跑/删除/从蓝图新建）。
 *
 * 后端接口就是既有的 /api/cron/jobs*，本页不新造契约；前端不做语法校验，
 * PUT 的 400 detail 原样显示（后端是唯一校验点）。列表默认 profile=all。
 */

const DIGEST_SCRIPT = "builtin:digest";
/** 立即跑之后多久自动刷新一次（下一个 tick 才会真正执行）。 */
const TRIGGER_REFRESH_MS = 30_000;

function errorText(err: unknown): string {
  const raw =
    err instanceof ApiError ? err.message : err instanceof Error ? err.message : String(err);
  return translateBackendMessage(raw, currentLang());
}

/** `builtin:digest` → `digest`；路径 → 文件名；其余原样。 */
function scriptLabel(script?: string | null): string {
  if (!script) return "—";
  if (script.startsWith("builtin:")) return script.slice("builtin:".length);
  const parts = script.split("/").filter(Boolean);
  return parts[parts.length - 1] || script;
}

/** 确定性的本地时间串（不走 toLocaleString：测试与跨时区都稳）。 */
function stamp(iso?: string | null): string {
  if (!iso) return "";
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())} ${pad(
    value.getHours(),
  )}:${pad(value.getMinutes())}`;
}

interface EditState {
  id: string;
  mode: "schedule" | "window";
  value: string;
  saving: boolean;
  error: string | null;
}

type Tone = "ok" | "error" | "muted";

function lastTone(job: CronJob): Tone {
  const status = (job.last_status ?? "").toLowerCase();
  if (status === "ok") return "ok";
  if (status) return "error";
  return "muted";
}

const TONE_DOT: Record<Tone, string> = {
  ok: "bg-[var(--vigil-ok)]",
  error: "bg-[var(--vigil-error)]",
  muted: "bg-[var(--vigil-muted)] opacity-50",
};

const TONE_TEXT: Record<Tone, string> = {
  ok: "text-[var(--vigil-ok)]",
  error: "text-[var(--vigil-error)]",
  muted: "text-[var(--vigil-muted)]",
};

function IconButton({
  label,
  onClick,
  disabled,
  tone = "default",
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  tone?: "default" | "danger";
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "inline-flex size-7 items-center justify-center rounded-md border border-[var(--vigil-border)]",
        "text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)]",
        "disabled:cursor-not-allowed disabled:opacity-40",
        tone === "danger" && "hover:text-[var(--vigil-error)]",
      )}
    >
      {children}
    </button>
  );
}

export default function SchedulesPage() {
  const { t } = useTranslation();
  const [jobs, setJobs] = useState<CronJob[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [edit, setEdit] = useState<EditState | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<CronJob | null>(null);
  const [deleting, setDeleting] = useState(false);

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [blueprints, setBlueprints] = useState<CronBlueprint[] | null>(null);
  const [blueprintError, setBlueprintError] = useState<string | null>(null);
  const [blueprintKey, setBlueprintKey] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const refreshTimer = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      const list = await api.listCronJobs("all");
      setJobs(Array.isArray(list) ? list : []);
      setLoadError(null);
    } catch (err) {
      setLoadError(errorText(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(
    () => () => {
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
    },
    [],
  );

  const patchJob = useCallback((job: CronJob) => {
    setJobs((prev) => (prev ?? []).map((item) => (item.id === job.id ? { ...item, ...job } : item)));
  }, []);

  const runRowAction = useCallback(
    async (job: CronJob, action: () => Promise<CronJob>) => {
      setBusyId(job.id);
      setRowError(null);
      setNotice(null);
      try {
        const updated = await action();
        if (updated && updated.id) patchJob(updated);
      } catch (err) {
        setRowError(`${t("schedules.actionFailed")}${errorText(err)}`);
      } finally {
        setBusyId(null);
      }
    },
    [patchJob, t],
  );

  const togglePause = (job: CronJob) =>
    void runRowAction(job, () =>
      job.enabled === false ? api.resumeCronJob(job.id) : api.pauseCronJob(job.id),
    );

  const runNow = (job: CronJob) =>
    void runRowAction(job, async () => {
      await api.runCronJobNow(job.id);
      setNotice(t("schedules.triggerStarted"));
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
      refreshTimer.current = window.setTimeout(() => void load(), 3000);
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
      refreshTimer.current = window.setTimeout(() => void load(), TRIGGER_REFRESH_MS);
      return job; // 立即跑不返回 job 新状态，等下一次列表刷新
    });

  const queueNext = (job: CronJob) => {
    void runRowAction(job, async () => {
      const updated = await api.triggerCronJob(job.id);
      setNotice(t("schedules.triggerQueued"));
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
      refreshTimer.current = window.setTimeout(() => void load(), TRIGGER_REFRESH_MS);
      return updated;
    });
  };

  const confirmDelete = async () => {
    const target = deleteTarget;
    if (!target) return;
    setDeleting(true);
    try {
      await api.deleteCronJob(target.id);
      setJobs((prev) => (prev ?? []).filter((item) => item.id !== target.id));
      setDeleteTarget(null);
    } catch (err) {
      setRowError(`${t("schedules.actionFailed")}${errorText(err)}`);
      setDeleteTarget(null);
    } finally {
      setDeleting(false);
    }
  };

  const openEdit = (job: CronJob, mode: EditState["mode"]) => {
    setRowError(null);
    setNotice(null);
    setEdit({
      id: job.id,
      mode,
      value: mode === "schedule" ? job.schedule_display ?? job.schedule?.display ?? "" : windowArg(job.script_args),
      saving: false,
      error: null,
    });
  };

  const saveEdit = async () => {
    if (!edit) return;
    const job = (jobs ?? []).find((item) => item.id === edit.id);
    const updates =
      edit.mode === "schedule"
        ? { schedule: edit.value.trim() }
        : { script_args: withWindowArg(job?.script_args, edit.value) };
    setEdit({ ...edit, saving: true, error: null });
    try {
      const updated = await api.updateCronJob(edit.id, updates);
      patchJob(updated);
      setEdit(null);
      setNotice(t("schedules.saved"));
    } catch (err) {
      setEdit((current) => (current ? { ...current, saving: false, error: errorText(err) } : current));
    }
  };

  const loadBlueprints = useCallback(async () => {
    try {
      const payload = await api.listBlueprints();
      const list = payload?.blueprints ?? [];
      setBlueprints(list);
      setBlueprintError(null);
      return list;
    } catch (err) {
      setBlueprintError(errorText(err));
      setBlueprints([]);
      return [];
    }
  }, []);

  const openCreate = async () => {
    setDrawerOpen(true);
    setCreateError(null);
    const list = blueprints ?? (await loadBlueprints());
    const first = list[0];
    if (first) selectBlueprint(first);
  };

  const selectBlueprint = (blueprint: CronBlueprint) => {
    setBlueprintKey(blueprint.key);
    const defaults: Record<string, string> = {};
    for (const field of blueprint.fields ?? []) {
      defaults[field.name] = field.default === undefined || field.default === null ? "" : String(field.default);
    }
    setValues(defaults);
    setCreateError(null);
  };

  const selectedBlueprint = (blueprints ?? []).find((item) => item.key === blueprintKey) ?? null;

  const submitCreate = async () => {
    if (!selectedBlueprint) return;
    setCreating(true);
    setCreateError(null);
    try {
      const job = await api.instantiateBlueprint(selectedBlueprint.key, values);
      setDrawerOpen(false);
      setNotice(t("schedules.createDone", { name: job?.name ?? selectedBlueprint.title }));
      await load();
    } catch (err) {
      setCreateError(`${t("schedules.createFailed")}${errorText(err)}`);
    } finally {
      setCreating(false);
    }
  };

  const hasJobs = (jobs?.length ?? 0) > 0;

  return (
    <div className="space-y-4 p-4" data-testid="schedules-page">
      <PageHeader
        icon={<CalendarClock className="size-5" />}
        eyebrow={t("schedules.eyebrow")}
        title={t("schedules.title")}
        description={t("schedules.description")}
        actions={
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void load()}
              className="inline-flex items-center gap-1.5 rounded-md border border-[var(--vigil-border)] px-2.5 py-1.5 text-xs text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)]"
            >
              <RefreshCw className="size-3.5" />
              {t("schedules.refresh")}
            </button>
            <button
              type="button"
              onClick={() => void openCreate()}
              className="inline-flex items-center gap-1.5 rounded-md bg-[var(--vigil-primary)] px-2.5 py-1.5 text-xs font-medium text-white hover:opacity-90"
            >
              <Plus className="size-3.5" />
              {t("schedules.newJob")}
            </button>
          </div>
        }
      />

      {loadError ? (
        <div className="flex items-center gap-3 rounded-md border border-[var(--vigil-error)] bg-[var(--vigil-error-bg)] px-3 py-2 text-xs text-[var(--vigil-error)]">
          <span className="min-w-0 flex-1 break-words">
            {t("schedules.loadFailed")}
            {loadError}
          </span>
          <button
            type="button"
            onClick={() => void load()}
            className="shrink-0 rounded border border-[var(--vigil-error)] px-2 py-0.5"
          >
            {t("schedules.retry")}
          </button>
        </div>
      ) : null}

      {rowError ? (
        <div className="rounded-md border border-[var(--vigil-error)] bg-[var(--vigil-error-bg)] px-3 py-2 text-xs text-[var(--vigil-error)]">
          {rowError}
        </div>
      ) : null}

      {notice && !rowError ? (
        <div className="flex items-center gap-2 rounded-md border border-[var(--vigil-ok)] bg-[var(--vigil-ok-bg)] px-3 py-2 text-xs text-[var(--vigil-ok)]">
          <Check className="size-3.5" />
          {notice}
        </div>
      ) : null}

      {jobs === null && !loadError ? (
        <div className="flex items-center gap-2 px-1 py-10 text-xs text-[var(--vigil-muted)]">
          <Loader2 className="size-4 animate-spin" />
          {t("schedules.loading")}
        </div>
      ) : null}

      {jobs !== null && !hasJobs ? (
        <EmptyState
          icon={<CalendarClock className="size-8" />}
          title={t("schedules.empty")}
          description={t("schedules.emptyHint")}
          action={
            <button
              type="button"
              onClick={() => void openCreate()}
              className="inline-flex items-center gap-1.5 rounded-md bg-[var(--vigil-primary)] px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
            >
              <Plus className="size-3.5" />
              {t("schedules.emptyAction")}
            </button>
          }
        />
      ) : null}

      {hasJobs ? (
        <div className="overflow-x-auto rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-card)]">
          <table className="w-full min-w-[880px] border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-[var(--vigil-border)] text-[10px] uppercase tracking-wide text-[var(--vigil-muted)]">
                <th className="px-3 py-2 font-medium">{t("schedules.colName")}</th>
                <th className="px-3 py-2 font-medium">{t("schedules.colSchedule")}</th>
                <th className="px-3 py-2 font-medium">{t("schedules.colScript")}</th>
                <th className="px-3 py-2 font-medium">{t("schedules.colNext")}</th>
                <th className="px-3 py-2 font-medium">{t("schedules.colLast")}</th>
                <th className="px-3 py-2 text-right font-medium">{t("schedules.colActions")}</th>
              </tr>
            </thead>
            <tbody>
              {(jobs ?? []).map((job) => {
                const paused = job.enabled === false;
                const digest = job.script === DIGEST_SCRIPT;
                const tone = lastTone(job);
                const busy = busyId === job.id;
                const editing = edit && edit.id === job.id ? edit : null;
                return (
                  <ScheduleRow
                    key={job.id}
                    job={job}
                    paused={paused}
                    digest={digest}
                    tone={tone}
                    busy={busy}
                    editing={editing}
                    onEdit={(mode) => openEdit(job, mode)}
                    onTogglePause={() => togglePause(job)}
                    onRunNow={() => runNow(job)}
                    onQueue={() => queueNext(job)}
                    onDelete={() => setDeleteTarget(job)}
                    onEditChange={(value) => setEdit((cur) => (cur ? { ...cur, value } : cur))}
                    onEditSave={() => void saveEdit()}
                    onEditCancel={() => setEdit(null)}
                  />
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}

      {deleteTarget ? (
        <div className="fixed inset-0 z-[90] flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/40" aria-hidden="true" onClick={() => setDeleteTarget(null)} />
          <div
            role="dialog"
            aria-modal="true"
            aria-label={t("schedules.deleteTitle")}
            className="relative w-full max-w-md rounded-lg border border-[var(--vigil-border)] bg-[var(--vigil-card)] p-4 shadow-2xl"
          >
            <div className="flex items-center gap-2 text-sm font-semibold text-[var(--vigil-text)]">
              <Trash2 className="size-4 text-[var(--vigil-error)]" />
              {t("schedules.deleteTitle")}
            </div>
            <p className="mt-2 text-xs text-[var(--vigil-muted)]">
              {t("schedules.deleteBody", { name: deleteTarget.name || deleteTarget.id })}
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setDeleteTarget(null)}
                className="rounded-md border border-[var(--vigil-border)] px-3 py-1.5 text-xs text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]"
              >
                {t("schedules.cancel")}
              </button>
              <button
                type="button"
                disabled={deleting}
                onClick={() => void confirmDelete()}
                className="inline-flex items-center gap-1.5 rounded-md bg-[var(--vigil-error)] px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
              >
                {deleting ? <Loader2 className="size-3.5 animate-spin" /> : <Trash2 className="size-3.5" />}
                {deleting ? t("schedules.deleting") : t("schedules.deleteConfirm")}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {drawerOpen ? (
        <div className="fixed inset-0 z-[90] flex justify-end" role="dialog" aria-label={t("schedules.newTitle")}>
          <div className="absolute inset-0 bg-black/40" aria-hidden="true" onClick={() => setDrawerOpen(false)} />
          <aside className="relative flex h-full w-full max-w-md flex-col border-l border-[var(--vigil-border)] bg-[var(--vigil-card)] shadow-[-8px_0_30px_rgba(0,0,0,0.18)]">
            <div className="flex items-center gap-2 border-b border-[var(--vigil-border)] px-4 py-3">
              <span className="flex-1 text-sm font-semibold text-[var(--vigil-text)]">{t("schedules.newTitle")}</span>
              <button
                type="button"
                aria-label={t("schedules.close")}
                onClick={() => setDrawerOpen(false)}
                className="inline-flex size-7 items-center justify-center rounded-md text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]"
              >
                <X className="size-4" />
              </button>
            </div>
            <div className="flex-1 space-y-3 overflow-y-auto px-4 py-3">
              {blueprintError ? (
                <div className="rounded-md border border-[var(--vigil-error)] bg-[var(--vigil-error-bg)] px-3 py-2 text-xs text-[var(--vigil-error)]">
                  {t("schedules.loadFailed")}
                  {blueprintError}
                </div>
              ) : null}
              <label className="block space-y-1">
                <span className="text-[11px] font-medium text-[var(--vigil-text)]">{t("schedules.blueprint")}</span>
                <select
                  aria-label={t("schedules.blueprint")}
                  value={blueprintKey}
                  onChange={(event) => {
                    const next = (blueprints ?? []).find((item) => item.key === event.target.value);
                    if (next) selectBlueprint(next);
                  }}
                  className="w-full rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-bg)] px-2 py-1.5 text-xs text-[var(--vigil-text)]"
                >
                  <option value="">{t("schedules.blueprintPlaceholder")}</option>
                  {(blueprints ?? []).map((blueprint) => (
                    <option key={blueprint.key} value={blueprint.key}>
                      {blueprint.title}
                    </option>
                  ))}
                </select>
              </label>

              {blueprints !== null && blueprints.length === 0 && !blueprintError ? (
                <div className="text-xs text-[var(--vigil-muted)]">{t("schedules.blueprintEmpty")}</div>
              ) : null}

              {selectedBlueprint ? (
                <>
                  {selectedBlueprint.description ? (
                    <p className="text-[11px] leading-relaxed text-[var(--vigil-muted)]">
                      {selectedBlueprint.description}
                    </p>
                  ) : null}
                  {(selectedBlueprint.fields ?? []).map((field) => (
                    <label key={field.name} className="block space-y-1">
                      <span className="text-[11px] font-medium text-[var(--vigil-text)]">
                        {field.label}
                        {field.optional ? null : <span className="ml-1 text-[var(--vigil-muted)]">*</span>}
                      </span>
                      {field.type === "enum" && (field.options?.length ?? 0) > 0 && field.strict !== false ? (
                        <select
                          aria-label={field.label}
                          name={field.name}
                          value={values[field.name] ?? ""}
                          onChange={(event) =>
                            setValues((prev) => ({ ...prev, [field.name]: event.target.value }))
                          }
                          className="w-full rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-bg)] px-2 py-1.5 text-xs text-[var(--vigil-text)]"
                        >
                          {(field.options ?? []).map((option) => (
                            <option key={option} value={option}>
                              {option}
                            </option>
                          ))}
                        </select>
                      ) : field.type === "enum" && (field.options?.length ?? 0) > 0 ? (
                        // strict=false：options 只是建议（如 digest 窗口 7d/14d/30d，
                        // 但 1d / 12h / 90d 也合法）——用 datalist 保留下拉又允许自由输入。
                        <>
                          <input
                            aria-label={field.label}
                            name={field.name}
                            list={`blueprint-${field.name}`}
                            value={values[field.name] ?? ""}
                            onChange={(event) =>
                              setValues((prev) => ({ ...prev, [field.name]: event.target.value }))
                            }
                            className="w-full rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-bg)] px-2 py-1.5 text-xs text-[var(--vigil-text)]"
                          />
                          <datalist id={`blueprint-${field.name}`}>
                            {(field.options ?? []).map((option) => (
                              <option key={option} value={option} />
                            ))}
                          </datalist>
                        </>
                      ) : (
                        <input
                          aria-label={field.label}
                          name={field.name}
                          value={values[field.name] ?? ""}
                          onChange={(event) =>
                            setValues((prev) => ({ ...prev, [field.name]: event.target.value }))
                          }
                          className="w-full rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-bg)] px-2 py-1.5 text-xs text-[var(--vigil-text)]"
                        />
                      )}
                      {field.help ? (
                        <span className="block text-[10px] text-[var(--vigil-muted)]">{field.help}</span>
                      ) : null}
                    </label>
                  ))}
                </>
              ) : null}

              {createError ? (
                <div className="rounded-md border border-[var(--vigil-error)] bg-[var(--vigil-error-bg)] px-3 py-2 text-xs text-[var(--vigil-error)]">
                  {createError}
                </div>
              ) : null}
            </div>
            <div className="flex justify-end gap-2 border-t border-[var(--vigil-border)] px-4 py-3">
              <button
                type="button"
                onClick={() => setDrawerOpen(false)}
                className="rounded-md border border-[var(--vigil-border)] px-3 py-1.5 text-xs text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]"
              >
                {t("schedules.cancel")}
              </button>
              <button
                type="button"
                disabled={!selectedBlueprint || creating}
                onClick={() => void submitCreate()}
                className="inline-flex items-center gap-1.5 rounded-md bg-[var(--vigil-primary)] px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
              >
                {creating ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />}
                {creating ? t("schedules.creating") : t("schedules.create")}
              </button>
            </div>
          </aside>
        </div>
      ) : null}
    </div>
  );
}

function ScheduleRow({
  job,
  paused,
  digest,
  tone,
  busy,
  editing,
  onEdit,
  onTogglePause,
  onRunNow,
  onQueue,
  onDelete,
  onEditChange,
  onEditSave,
  onEditCancel,
}: {
  job: CronJob;
  paused: boolean;
  digest: boolean;
  tone: Tone;
  busy: boolean;
  editing: EditState | null;
  onEdit: (mode: EditState["mode"]) => void;
  onTogglePause: () => void;
  onRunNow: () => void;
  onQueue: () => void;
  onDelete: () => void;
  onEditChange: (value: string) => void;
  onEditSave: () => void;
  onEditCancel: () => void;
}) {
  const { t } = useTranslation();
  const next = paused ? t("schedules.paused") : stamp(job.next_run_at) || "—";
  const showWindow = digest;
  return (
    <>
      <tr className="border-b border-[var(--vigil-border)] align-middle last:border-b-0">
        <td className="px-3 py-2">
          <div className="flex items-center gap-1.5">
            <span className="truncate font-medium text-[var(--vigil-text)]">{job.name || job.id}</span>
            {job.no_agent ? (
              <span className="rounded border border-[var(--vigil-border)] px-1 py-px text-[10px] text-[var(--vigil-muted)]">
                {t("schedules.scriptBadge")}
              </span>
            ) : null}
          </div>
          {job.profile && job.profile !== "default" ? (
            <div className="text-[10px] text-[var(--vigil-muted)]">{job.profile}</div>
          ) : null}
        </td>
        <td className="px-3 py-2">
          <code className="rounded bg-[var(--vigil-muted-bg)] px-1.5 py-0.5 font-mono text-[11px]">
            {job.schedule_display || job.schedule?.display || "—"}
          </code>
        </td>
        <td className="px-3 py-2 font-mono text-[11px] text-[var(--vigil-muted)]" title={job.script ?? undefined}>
          {scriptLabel(job.script)}
        </td>
        <td className="px-3 py-2">
          {paused ? (
            <span className="text-[var(--vigil-muted)]">{next}</span>
          ) : job.next_run_at ? (
            <time dateTime={job.next_run_at} className="text-[var(--vigil-text)]">
              {next}
            </time>
          ) : (
            <span className="text-[var(--vigil-muted)]">—</span>
          )}
        </td>
        <td className="px-3 py-2">
          <div className="flex items-center gap-1.5">
            <span className={cn("inline-block size-1.5 rounded-full", TONE_DOT[tone])} />
            <span className={cn("text-[11px]", TONE_TEXT[tone])}>
              {tone === "ok"
                ? t("schedules.statusOk")
                : tone === "error"
                  ? t("schedules.statusError")
                  : t("schedules.neverRun")}
            </span>
            {job.last_run_at ? (
              <span className="text-[10px] text-[var(--vigil-muted)]">{stamp(job.last_run_at)}</span>
            ) : null}
          </div>
        </td>
        <td className="px-3 py-2">
          <div className="flex items-center justify-end gap-1">
            <button
              type="button"
              onClick={() => onEdit("schedule")}
              disabled={busy}
              className="rounded-md border border-[var(--vigil-border)] px-2 py-1 text-[11px] text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)] disabled:opacity-40"
            >
              {t("schedules.editSchedule")}
            </button>
            {showWindow ? (
              <button
                type="button"
                onClick={() => onEdit("window")}
                disabled={busy}
                className="rounded-md border border-[var(--vigil-border)] px-2 py-1 text-[11px] text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)] disabled:opacity-40"
              >
                {t("schedules.editWindow")}
              </button>
            ) : null}
            <IconButton label={paused ? t("schedules.resume") : t("schedules.pause")} onClick={onTogglePause} disabled={busy}>
              {paused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
            </IconButton>
            <IconButton label={paused ? t("schedules.runNowPaused") : t("schedules.runNow")}
              onClick={onRunNow} disabled={busy || paused}>
              {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Zap className="size-3.5" />}
            </IconButton>
            <IconButton label={paused ? t("schedules.queuePaused") : t("schedules.queueNext")}
              onClick={onQueue} disabled={busy || paused}>
              <Timer className="size-3.5" />
            </IconButton>
            <IconButton label={t("schedules.delete")} onClick={onDelete} disabled={busy} tone="danger">
              <Trash2 className="size-3.5" />
            </IconButton>
          </div>
        </td>
      </tr>
      {editing ? (
        <tr className="border-b border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)]">
          <td colSpan={6} className="px-3 py-3">
            <EditPanel
              editing={editing}
              onChange={onEditChange}
              onSave={onEditSave}
              onCancel={onEditCancel}
            />
          </td>
        </tr>
      ) : null}
    </>
  );
}

function EditPanel({
  editing,
  onChange,
  onSave,
  onCancel,
}: {
  editing: EditState;
  onChange: (value: string) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const isWindow = editing.mode === "window";
  const chips = isWindow
    ? ["7d", "1d", "24h", "2w"]
    : [t("schedules.scheduleChipInterval"), t("schedules.scheduleChipCron"), t("schedules.scheduleChipOnce")];
  return (
    <div className="space-y-2" data-testid={`schedule-edit-${editing.mode}`}>
      <label className="block space-y-1">
        <span className="text-[11px] font-medium text-[var(--vigil-text)]">
          {isWindow ? t("schedules.windowLabel") : t("schedules.scheduleLabel")}
        </span>
        <input
          aria-label={isWindow ? t("schedules.windowLabel") : t("schedules.scheduleLabel")}
          value={editing.value}
          placeholder={isWindow ? t("schedules.windowPlaceholder") : t("schedules.schedulePlaceholder")}
          onChange={(event) => onChange(event.target.value)}
          className="w-full max-w-md rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-bg)] px-2 py-1.5 font-mono text-xs text-[var(--vigil-text)]"
        />
      </label>
      {!isWindow ? (
        <div className="flex flex-wrap gap-1.5">
          {chips.map((chip) => (
            <button
              key={chip}
              type="button"
              onClick={() => onChange(chip)}
              className="rounded-full border border-[var(--vigil-border)] px-2 py-0.5 font-mono text-[10px] text-[var(--vigil-muted)] hover:bg-[var(--vigil-card)] hover:text-[var(--vigil-text)]"
            >
              {chip}
            </button>
          ))}
        </div>
      ) : null}
      <p className="max-w-2xl text-[10px] leading-relaxed text-[var(--vigil-muted)]">
        {isWindow ? t("schedules.windowHint") : t("schedules.scheduleHint")}
      </p>
      {editing.error ? (
        <div className="max-w-2xl rounded-md border border-[var(--vigil-error)] bg-[var(--vigil-error-bg)] px-2 py-1.5 text-[11px] text-[var(--vigil-error)]">
          {editing.error}
        </div>
      ) : null}
      <div className="flex gap-2">
        <button
          type="button"
          disabled={editing.saving}
          onClick={onSave}
          className="inline-flex items-center gap-1.5 rounded-md bg-[var(--vigil-primary)] px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
        >
          {editing.saving ? <Loader2 className="size-3.5 animate-spin" /> : null}
          {editing.saving ? t("schedules.saving") : t("schedules.save")}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-[var(--vigil-border)] px-3 py-1.5 text-xs text-[var(--vigil-muted)] hover:bg-[var(--vigil-card)]"
        >
          {t("schedules.cancel")}
        </button>
      </div>
    </div>
  );
}
