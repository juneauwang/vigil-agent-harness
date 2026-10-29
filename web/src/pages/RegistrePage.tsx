import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import "@/i18n";
import { currentLang } from "@/i18n";
import {
  BookMarked,
  Check,
  ClipboardCopy,
  ExternalLink,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  X,
  Zap,
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
import type {
  CronJob,
  DigestConfig,
  DigestConfigPatch,
  DigestReport,
  DigestRun,
  DigestTestResult,
} from "@/lib/api";
import { translateBackendMessage } from "@/lib/backendMsg";
import { withWindowArg, windowArg } from "@/lib/schedules";
import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { Markdown } from "@/components/Markdown";
import { cn } from "@/lib/ops";

/**
 * Le Registre（D3）：digest 报告流水线 + 发布设置 + 台账。
 *
 * 与 /schedules 的分工：/schedules 只管通用 cron；报告流水线（写稿 → 渲染发布）
 * 是「一条流水线」，计划/窗口/发布端/凭据都在这一页配。报告本体与对象口径
 * 由后端决定，这里不重算。
 */

const DIGEST_SCRIPT = "builtin:digest";
const POLL_MS = 3000;
const GENERATE_TIMEOUT_MS = 120_000;

function errorText(err: unknown): string {
  const raw =
    err instanceof ApiError ? err.message : err instanceof Error ? err.message : String(err);
  return translateBackendMessage(raw, currentLang());
}

function formatOf(job?: CronJob | null): "facts" | "md" | "" {
  const args = (job?.script_args ?? []).map(String);
  const index = args.indexOf("--format");
  const value = index >= 0 && index + 1 < args.length ? args[index + 1] : "";
  return value === "facts" || value === "md" ? value : "";
}

function pickJob(jobs: CronJob[], format: "facts" | "md"): CronJob | null {
  return (
    jobs.find((job) => String(job.script ?? "") === DIGEST_SCRIPT && formatOf(job) === format) ?? null
  );
}

function stamp(iso?: string | null): string {
  if (!iso) return "";
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())} ${pad(
    value.getHours(),
  )}:${pad(value.getMinutes())}`;
}

function formatBytes(size: number): string {
  if (!size) return "0 B";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

/** BookStack slug：文件名去 .md 的「小写 ASCII 部分」，连续非字母数字折叠成 -。 */
export function bookstackSlug(name: string): string {
  return name
    .replace(/\.md$/i, "")
    .replace(/[^\x20-\x7E]/g, "")
    .replace(/[^A-Za-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase();
}

function isTerminal(status: string): boolean {
  return status === "completed" || status === "failed" || status === "unknown";
}

interface EditState {
  id: string;
  mode: "schedule" | "window";
  value: string;
  saving: boolean;
  error: string | null;
}

interface SettingsForm {
  target: string;
  base_url: string;
  book_id: string;
  credential_ref: string;
  token: string;
}

function formFromConfig(config: DigestConfig | null): SettingsForm {
  const bookstack = config?.publish?.bookstack;
  return {
    target: config?.publish?.target ?? "none",
    base_url: bookstack?.base_url ?? "",
    book_id: bookstack?.book_id ? String(bookstack.book_id) : "",
    credential_ref: bookstack?.credential_ref || "bookstack-api",
    token: "",
  };
}

export default function RegistrePage() {
  const { t } = useTranslation();

  const [jobs, setJobs] = useState<CronJob[] | null>(null);
  const [config, setConfig] = useState<DigestConfig | null>(null);
  const [reports, setReports] = useState<DigestReport[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [edit, setEdit] = useState<EditState | null>(null);
  const [syncing, setSyncing] = useState(false);

  const [generating, setGenerating] = useState(false);
  const [runs, setRuns] = useState<DigestRun[]>([]);

  const [form, setForm] = useState<SettingsForm>(formFromConfig(null));
  const [savingSettings, setSavingSettings] = useState(false);
  const [settingsMsg, setSettingsMsg] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<DigestTestResult | null>(null);

  const [preview, setPreview] = useState<{ name: string; content: string } | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);

  // 一键初始化：没有流水线时给的最小表单（窗口 + 周几 + 几点，不让用户碰 cron 语法）
  const [createForm, setCreateForm] = useState({ window: "7d", dow: "5", hour: "18" });
  const [creating, setCreating] = useState(false);

  const pollTimer = useRef<number | null>(null);
  const copyTimer = useRef<number | null>(null);

  const clearTimers = useCallback(() => {
    if (pollTimer.current !== null) window.clearTimeout(pollTimer.current);
    pollTimer.current = null;
    if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    copyTimer.current = null;
  }, []);

  useEffect(() => clearTimers, [clearTimers]);

  const load = useCallback(async () => {
    try {
      const [jobsList, configPayload, reportsPayload] = await Promise.all([
        api.listCronJobs("all"),
        api.getDigestConfig(),
        api.listDigestReports(),
      ]);
      setJobs(Array.isArray(jobsList) ? jobsList : []);
      const cfg = configPayload?.data ?? null;
      setConfig(cfg);
      setForm(formFromConfig(cfg));
      setReports(reportsPayload?.data ?? []);
      setLoadError(null);
    } catch (err) {
      setLoadError(errorText(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const reloadJobs = useCallback(async () => {
    try {
      const list = await api.listCronJobs("all");
      setJobs(Array.isArray(list) ? list : []);
    } catch (err) {
      setActionError(`${t("registre.actionFailed")}${errorText(err)}`);
    }
  }, [t]);

  const reloadReports = useCallback(async () => {
    try {
      const payload = await api.listDigestReports();
      setReports(payload?.data ?? []);
    } catch {
      /* 台账刷新失败不覆盖主状态 */
    }
  }, []);

  const factsJob = useMemo(() => pickJob(jobs ?? [], "facts"), [jobs]);
  const renderJob = useMemo(() => pickJob(jobs ?? [], "md"), [jobs]);
  const factsWindow = windowArg(factsJob?.script_args);
  const renderWindow = windowArg(renderJob?.script_args);
  const windowsDiffer = Boolean(factsJob && renderJob && factsWindow !== renderWindow);
  const pipelineMissing = !factsJob || !renderJob;
  const latestReport = reports[0] ?? null;

  const openEdit = (job: CronJob, mode: EditState["mode"]) => {
    setActionError(null);
    setNotice(null);
    setEdit({
      id: job.id,
      mode,
      value:
        mode === "schedule"
          ? job.schedule_display ?? job.schedule?.display ?? ""
          : windowArg(job.script_args),
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
      await api.updateCronJob(edit.id, updates);
      await reloadJobs();
      setEdit(null);
    } catch (err) {
      setEdit((cur) => (cur ? { ...cur, saving: false, error: errorText(err) } : cur));
    }
  };

  const togglePause = async (job: CronJob) => {
    setActionError(null);
    try {
      const updated = job.enabled === false ? await api.resumeCronJob(job.id) : await api.pauseCronJob(job.id);
      if (updated?.id) await reloadJobs();
    } catch (err) {
      setActionError(`${t("registre.actionFailed")}${errorText(err)}`);
    }
  };

  const syncWindows = async () => {
    if (!factsJob || !renderJob) return;
    const target = factsWindow || renderWindow;
    if (!target) return;
    setSyncing(true);
    setActionError(null);
    setNotice(null);
    try {
      // 「同步到另一条」= 一次把两条都写成同一个窗口（PUT 两条，语义明确）。
      for (const job of [factsJob, renderJob]) {
        await api.updateCronJob(job.id, {
          script_args: withWindowArg(job.script_args, target),
        });
      }
      await reloadJobs();
      setNotice(t("registre.syncWindowOk", { window: target }));
    } catch (err) {
      setActionError(`${t("registre.actionFailed")}${errorText(err)}`);
    } finally {
      setSyncing(false);
    }
  };

  const createPipeline = async () => {
    if (creating) return;
    const window = createForm.window.trim() || "7d";
    const hour = createForm.hour.trim() || "18";
    const dow = createForm.dow;
    setCreating(true);
    setActionError(null);
    setNotice(null);
    try {
      const payload = await api.createDigestPipeline({
        window,
        facts_schedule: `0 ${hour} * * ${dow}`,
        render_schedule: `10 ${hour} * * ${dow}`,
      });
      const data = payload?.data;
      await reloadJobs();
      setNotice(
        data
          ? t("registre.pipelineCreated", {
              facts: `${data.facts.schedule} (${data.facts.id})`,
              render: `${data.render.schedule} (${data.render.id})`,
            })
          : t("registre.createPipeline"),
      );
    } catch (err) {
      setActionError(`${t("registre.pipelineCreateFailed")}${errorText(err)}`);
    } finally {
      setCreating(false);
    }
  };

  const generate = async () => {
    if (generating) return;
    const startedAt = Date.now();
    setGenerating(true);
    setActionError(null);
    setNotice(t("registre.generateQueued"));
    try {
      await api.generateDigest();
    } catch (err) {
      setActionError(`${t("registre.actionFailed")}${errorText(err)}`);
      setGenerating(false);
      return;
    }
    const factsId = factsJob?.id ?? "";
    const renderId = renderJob?.id ?? "";
    const poll = async () => {
      let finished = true;
      try {
        const payload = await api.listDigestRuns();
        const rows = payload?.data ?? [];
        setRuns(rows);
        const fresh = rows.filter((row) => {
          const at = Date.parse(row.claimed_at);
          return Number.isNaN(at) ? true : at >= startedAt;
        });
        const done = (jobId: string) =>
          !jobId || fresh.some((row) => row.job_id === jobId && isTerminal(row.status));
        finished = done(factsId) && done(renderId) && fresh.length > 0;
      } catch {
        finished = false;
      }
      if (finished || Date.now() - startedAt > GENERATE_TIMEOUT_MS) {
        setGenerating(false);
        if (!finished) setNotice(t("registre.generateTimeout"));
        await reloadReports();
        return;
      }
      pollTimer.current = window.setTimeout(() => void poll(), POLL_MS);
    };
    pollTimer.current = window.setTimeout(() => void poll(), POLL_MS);
  };

  const saveSettings = async () => {
    setSavingSettings(true);
    setSettingsMsg(null);
    const ref = form.credential_ref.trim() || "bookstack-api";
    try {
      const patch: DigestConfigPatch = {
        target: form.target,
        base_url: form.base_url.trim(),
        book_id: Number(form.book_id || 0),
        credential_ref: ref,
      };
      const saved = await api.updateDigestConfig(patch);
      if (saved?.data) setConfig(saved.data);

      const token = form.token.trim();
      if (token) {
        // 只有填了令牌才写 vault；留空 = 不改（值永不回显）。
        await api.setDigestCredential(ref, token);
        setForm((current) => ({ ...current, token: "" }));
        setConfig((current) =>
          current
            ? {
                ...current,
                publish: {
                  ...current.publish,
                  bookstack: { ...current.publish.bookstack, configured: true, credential_ref: ref },
                },
              }
            : current,
        );
        const refreshed = await api.getDigestConfig().catch(() => null);
        if (refreshed?.data) setConfig(refreshed.data);
      }
      setSettingsMsg({ tone: "ok", text: t("registre.settingsSaved") });
    } catch (err) {
      setSettingsMsg({ tone: "error", text: errorText(err) });
    } finally {
      setSavingSettings(false);
    }
  };

  const testConnection = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await api.testDigestPublish({
        base_url: form.base_url.trim(),
        book_id: Number(form.book_id || 0),
        credential_ref: form.credential_ref.trim() || "bookstack-api",
      });
      setTestResult(result);
    } catch (err) {
      setTestResult({ ok: false, detail: errorText(err) });
    } finally {
      setTesting(false);
    }
  };

  const openPreview = async (report: DigestReport) => {
    setPreview({ name: report.name, content: "" });
    setPreviewError(null);
    setPreviewLoading(true);
    try {
      const payload = await api.getDigestReport(report.name);
      setPreview({ name: report.name, content: payload?.data?.content ?? "" });
    } catch (err) {
      setPreviewError(errorText(err));
    } finally {
      setPreviewLoading(false);
    }
  };

  const copyPath = (report: DigestReport) => {
    const value = report.path || report.name;
    try {
      void navigator.clipboard?.writeText(value);
    } catch {
      /* 剪贴板不可用（jsdom/权限）时静默 */
    }
    setCopied(report.name);
    if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => setCopied(null), 2000);
  };

  // URL 只认发布账本里的真值（BookStack 的 book 路由认 slug 不认 id，
  // 前端拼 /books/<id>/page/<slug> 会得到死链）。
  const linkFor = (report: DigestReport): string => (report.url ?? "").trim();

  return (
    <div className="space-y-4 p-4" data-testid="registre-page">
      <PageHeader
        icon={<BookMarked className="size-5" />}
        eyebrow={t("registre.eyebrow")}
        title={t("registre.title")}
        description={t("registre.description")}
        actions={
          <button
            type="button"
            onClick={() => void load()}
            className="inline-flex items-center gap-1.5 rounded-md border border-[var(--vigil-border)] px-2.5 py-1.5 text-xs text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)]"
          >
            <RefreshCw className="size-3.5" />
            {t("schedules.refresh")}
          </button>
        }
      />

      {loadError ? (
        <div className="flex items-center gap-3 rounded-md border border-[var(--vigil-error)] bg-[var(--vigil-error-bg)] px-3 py-2 text-xs text-[var(--vigil-error)]">
          <span className="min-w-0 flex-1 break-words">
            {t("registre.loadFailed")}
            {loadError}
          </span>
          <button
            type="button"
            onClick={() => void load()}
            className="shrink-0 rounded border border-[var(--vigil-error)] px-2 py-0.5"
          >
            {t("registre.retry")}
          </button>
        </div>
      ) : null}

      {actionError ? (
        <div className="rounded-md border border-[var(--vigil-error)] bg-[var(--vigil-error-bg)] px-3 py-2 text-xs text-[var(--vigil-error)]">
          {actionError}
        </div>
      ) : null}

      {notice && !actionError ? (
        <div className="flex items-center gap-2 rounded-md border border-[var(--vigil-ok)] bg-[var(--vigil-ok-bg)] px-3 py-2 text-xs text-[var(--vigil-ok)]">
          <Check className="size-3.5" />
          {notice}
        </div>
      ) : null}

      {/* ── 1. 周报流水线卡 ─────────────────────────────────────────── */}
      <section
        data-testid="registre-pipeline"
        className={cn(
          "rounded-lg border bg-[var(--vigil-card)]",
          windowsDiffer ? "border-[var(--vigil-warn)]" : "border-[var(--vigil-border)]",
        )}
      >
        <div className="flex flex-wrap items-start gap-2 border-b border-[var(--vigil-border)] px-4 py-3">
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold text-[var(--vigil-text)]">{t("registre.pipelineTitle")}</div>
            <div className="mt-0.5 text-[11px] text-[var(--vigil-muted)]">{t("registre.pipelineHint")}</div>
          </div>
          {windowsDiffer ? (
            <button
              type="button"
              onClick={() => void syncWindows()}
              disabled={syncing}
              className="inline-flex items-center gap-1.5 rounded-md border border-[var(--vigil-warn)] px-2.5 py-1 text-[11px] font-medium text-[var(--vigil-warn)] hover:bg-[var(--vigil-muted-bg)] disabled:opacity-50"
            >
              {syncing ? <Loader2 className="size-3.5 animate-spin" /> : null}
              {t("registre.syncWindow")}
            </button>
          ) : null}
        </div>

        {pipelineMissing ? (
          <div className="space-y-3 px-4 py-4" data-testid="registre-pipeline-empty">
            <div>
              <div className="text-xs font-medium text-[var(--vigil-text)]">
                {t("registre.pipelineEmptyTitle")}
              </div>
              <div className="mt-0.5 text-[11px] leading-relaxed text-[var(--vigil-muted)]">
                {t("registre.pipelineEmptyHint")}
              </div>
            </div>
            <div className="flex flex-wrap items-end gap-3">
              <label className="block space-y-1">
                <span className="text-[11px] font-medium text-[var(--vigil-text)]">
                  {t("registre.createWindow")}
                </span>
                <input
                  aria-label={t("registre.createWindow")}
                  value={createForm.window}
                  placeholder="7d"
                  onChange={(event) =>
                    setCreateForm((cur) => ({ ...cur, window: event.target.value }))
                  }
                  className="w-24 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-bg)] px-2 py-1.5 font-mono text-xs text-[var(--vigil-text)]"
                />
              </label>
              <label className="block space-y-1">
                <span className="text-[11px] font-medium text-[var(--vigil-text)]">
                  {t("registre.createDow")}
                </span>
                <select
                  aria-label={t("registre.createDow")}
                  value={createForm.dow}
                  onChange={(event) =>
                    setCreateForm((cur) => ({ ...cur, dow: event.target.value }))
                  }
                  className="rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-bg)] px-2 py-1.5 text-xs text-[var(--vigil-text)]"
                >
                  <option value="1">{t("registre.dowMon")}</option>
                  <option value="2">{t("registre.dowTue")}</option>
                  <option value="3">{t("registre.dowWed")}</option>
                  <option value="4">{t("registre.dowThu")}</option>
                  <option value="5">{t("registre.dowFri")}</option>
                  <option value="6">{t("registre.dowSat")}</option>
                  <option value="0">{t("registre.dowSun")}</option>
                </select>
              </label>
              <label className="block space-y-1">
                <span className="text-[11px] font-medium text-[var(--vigil-text)]">
                  {t("registre.createHour")}
                </span>
                <input
                  aria-label={t("registre.createHour")}
                  type="number"
                  min={0}
                  max={23}
                  value={createForm.hour}
                  onChange={(event) =>
                    setCreateForm((cur) => ({ ...cur, hour: event.target.value }))
                  }
                  className="w-20 rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-bg)] px-2 py-1.5 font-mono text-xs text-[var(--vigil-text)]"
                />
              </label>
              <button
                type="button"
                onClick={() => void createPipeline()}
                disabled={creating}
                className="inline-flex items-center gap-1.5 rounded-md bg-[var(--vigil-primary)] px-3 py-1.5 text-[11px] font-medium text-white hover:opacity-90 disabled:opacity-50"
              >
                {creating ? <Loader2 className="size-3.5 animate-spin" /> : null}
                {creating ? t("registre.creatingPipeline") : t("registre.createPipeline")}
              </button>
            </div>
            <div className="text-[11px] text-[var(--vigil-muted)]">
              {t("registre.createPipelineHint", { hour: createForm.hour || "18" })}
            </div>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] border-collapse text-left text-xs">
              <thead>
                <tr className="border-b border-[var(--vigil-border)] text-[10px] uppercase tracking-wide text-[var(--vigil-muted)]">
                  <th className="px-4 py-2 font-medium" />
                  <th className="px-3 py-2 font-medium">{t("schedules.colSchedule")}</th>
                  <th className="px-3 py-2 font-medium">{t("registre.windowLabel")}</th>
                  <th className="px-3 py-2 font-medium">{t("registre.nextLabel")}</th>
                  <th className="px-3 py-2 font-medium">{t("registre.lastLabel")}</th>
                  <th className="px-3 py-2 text-right font-medium">{t("schedules.colActions")}</th>
                </tr>
              </thead>
              <tbody>
                {[
                  { key: "facts", label: t("registre.writeRow"), job: factsJob },
                  { key: "render", label: t("registre.renderRow"), job: renderJob },
                ].map(({ key, label, job }) => {
                  if (!job) return null;
                  const paused = job.enabled === false;
                  const editing = edit && edit.id === job.id ? edit : null;
                  return (
                    <PipelineRow
                      key={key}
                      rowKey={key}
                      label={label}
                      job={job}
                      paused={paused}
                      editing={editing}
                      onEdit={(mode) => openEdit(job, mode)}
                      onTogglePause={() => void togglePause(job)}
                      onEditChange={(value) => setEdit((cur) => (cur ? { ...cur, value } : cur))}
                      onEditSave={() => void saveEdit()}
                      onEditCancel={() => setEdit(null)}
                    />
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <div className="flex flex-wrap items-center gap-3 border-t border-[var(--vigil-border)] px-4 py-3">
          <button
            type="button"
            onClick={() => void generate()}
            disabled={generating || pipelineMissing}
            className="inline-flex items-center gap-1.5 rounded-md bg-[var(--vigil-primary)] px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            {generating ? <Loader2 className="size-3.5 animate-spin" /> : <Zap className="size-3.5" />}
            {generating ? t("registre.generating") : t("registre.generate")}
          </button>
          <span className="text-[11px] text-[var(--vigil-muted)]">{t("registre.generateHint")}</span>
        </div>

        <div className="border-t border-[var(--vigil-border)] px-4 py-3 text-xs">
          <div className="text-[10px] uppercase tracking-wide text-[var(--vigil-muted)]">
            {t("registre.statusTitle")}
          </div>
          {latestReport ? (
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <span className="font-mono text-[11px] text-[var(--vigil-text)]">{latestReport.name}</span>
              <span className="text-[11px] text-[var(--vigil-muted)]">{stamp(latestReport.mtime)}</span>
              <span className="text-[11px] text-[var(--vigil-muted)]">
                {config?.publish?.target === "bookstack"
                  ? t("registre.statusPosted")
                  : config?.publish?.target === "none"
                    ? t("registre.statusNoneTarget")
                    : t("registre.statusFile")}
              </span>
              {runs[0] ? (
                <span className="rounded border border-[var(--vigil-border)] px-1.5 py-px text-[10px] text-[var(--vigil-muted)]">
                  {runs[0].job_name || runs[0].job_id} · {runs[0].status}
                </span>
              ) : null}
            </div>
          ) : (
            <div className="mt-1 text-[11px] text-[var(--vigil-muted)]">{t("registre.statusNone")}</div>
          )}
        </div>
      </section>

      {/* ── 2. 发布设置卡 ───────────────────────────────────────────── */}
      <section
        data-testid="registre-settings"
        className="rounded-lg border border-[var(--vigil-border)] bg-[var(--vigil-card)]"
      >
        <div className="border-b border-[var(--vigil-border)] px-4 py-3">
          <div className="text-sm font-semibold text-[var(--vigil-text)]">{t("registre.settingsTitle")}</div>
          <div className="mt-0.5 text-[11px] text-[var(--vigil-muted)]">{t("registre.settingsHint")}</div>
        </div>
        <div className="grid gap-3 px-4 py-3 sm:grid-cols-2">
          <label className="block space-y-1">
            <span className="text-[11px] font-medium text-[var(--vigil-text)]">{t("registre.fldTarget")}</span>
            <select
              aria-label={t("registre.fldTarget")}
              value={form.target}
              onChange={(event) => setForm((cur) => ({ ...cur, target: event.target.value }))}
              className="w-full rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-bg)] px-2 py-1.5 text-xs text-[var(--vigil-text)]"
            >
              <option value="none">{t("registre.targetNone")}</option>
              <option value="file">{t("registre.targetFile")}</option>
              <option value="bookstack">{t("registre.targetBookstack")}</option>
            </select>
          </label>
          <label className="block space-y-1">
            <span className="text-[11px] font-medium text-[var(--vigil-text)]">{t("registre.fldBaseUrl")}</span>
            <input
              aria-label={t("registre.fldBaseUrl")}
              value={form.base_url}
              placeholder="http://127.0.0.1:8080"
              onChange={(event) => setForm((cur) => ({ ...cur, base_url: event.target.value }))}
              className="w-full rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-bg)] px-2 py-1.5 font-mono text-xs text-[var(--vigil-text)]"
            />
          </label>
          <label className="block space-y-1">
            <span className="text-[11px] font-medium text-[var(--vigil-text)]">{t("registre.fldBookId")}</span>
            <input
              aria-label={t("registre.fldBookId")}
              value={form.book_id}
              inputMode="numeric"
              placeholder="1"
              onChange={(event) => setForm((cur) => ({ ...cur, book_id: event.target.value }))}
              className="w-full rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-bg)] px-2 py-1.5 font-mono text-xs text-[var(--vigil-text)]"
            />
          </label>
          <label className="block space-y-1">
            <span className="text-[11px] font-medium text-[var(--vigil-text)]">{t("registre.fldCredentialRef")}</span>
            <input
              aria-label={t("registre.fldCredentialRef")}
              value={form.credential_ref}
              placeholder="bookstack-api"
              onChange={(event) => setForm((cur) => ({ ...cur, credential_ref: event.target.value }))}
              className="w-full rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-bg)] px-2 py-1.5 font-mono text-xs text-[var(--vigil-text)]"
            />
          </label>
          <label className="block space-y-1">
            <span className="text-[11px] font-medium text-[var(--vigil-text)]">{t("registre.fldToken")}</span>
            <input
              aria-label={t("registre.fldToken")}
              type="password"
              value={form.token}
              autoComplete="new-password"
              placeholder={t("registre.tokenPlaceholder", {
                configured: config?.publish?.bookstack?.configured
                  ? t("registre.tokenConfigured")
                  : t("registre.tokenNotConfigured"),
              })}
              onChange={(event) => setForm((cur) => ({ ...cur, token: event.target.value }))}
              className="w-full rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-bg)] px-2 py-1.5 font-mono text-xs text-[var(--vigil-text)]"
            />
            <span className="block text-[10px] text-[var(--vigil-muted)]">
              {config?.publish?.bookstack?.configured
                ? t("registre.tokenConfigured")
                : t("registre.tokenNotConfigured")}
            </span>
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-2 border-t border-[var(--vigil-border)] px-4 py-3">
          <button
            type="button"
            onClick={() => void saveSettings()}
            disabled={savingSettings}
            className="inline-flex items-center gap-1.5 rounded-md bg-[var(--vigil-primary)] px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            {savingSettings ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />}
            {savingSettings ? t("registre.saving") : t("registre.saveSettings")}
          </button>
          <button
            type="button"
            onClick={() => void testConnection()}
            disabled={testing}
            className="inline-flex items-center gap-1.5 rounded-md border border-[var(--vigil-border)] px-3 py-1.5 text-xs text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)] disabled:opacity-50"
          >
            {testing ? <Loader2 className="size-3.5 animate-spin" /> : <ExternalLink className="size-3.5" />}
            {testing ? t("registre.testing") : t("registre.testConnection")}
          </button>
          {settingsMsg ? (
            <span
              className={cn(
                "text-[11px]",
                settingsMsg.tone === "ok" ? "text-[var(--vigil-ok)]" : "text-[var(--vigil-error)]",
              )}
            >
              {settingsMsg.text}
            </span>
          ) : null}
        </div>
        {testResult ? (
          <div
            data-testid="registre-test-result"
            className={cn(
              "mx-4 mb-3 rounded-md border px-3 py-2 text-[11px]",
              testResult.ok
                ? "border-[var(--vigil-ok)] bg-[var(--vigil-ok-bg)] text-[var(--vigil-ok)]"
                : "border-[var(--vigil-error)] bg-[var(--vigil-error-bg)] text-[var(--vigil-error)]",
            )}
          >
            {testResult.ok
              ? `${testResult.status ?? 200} · ${testResult.detail || testResult.book_name || ""}`
              : `${testResult.status ?? "—"} · ${testResult.detail || ""}`}
          </div>
        ) : null}
      </section>

      {/* ── 3. 台账 ────────────────────────────────────────────────── */}
      <section
        data-testid="registre-ledger"
        className="rounded-lg border border-[var(--vigil-border)] bg-[var(--vigil-card)]"
      >
        <div className="flex items-center gap-2 border-b border-[var(--vigil-border)] px-4 py-3">
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold text-[var(--vigil-text)]">{t("registre.ledgerTitle")}</div>
            <div className="mt-0.5 text-[11px] text-[var(--vigil-muted)]">{t("registre.ledgerHint")}</div>
          </div>
        </div>

        {reports.length === 0 ? (
          <EmptyState
            icon={<BookMarked className="size-8" />}
            title={t("registre.ledgerEmpty")}
            className="mx-4 my-4"
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[820px] border-collapse text-left text-xs">
              <thead>
                <tr className="border-b border-[var(--vigil-border)] text-[10px] uppercase tracking-wide text-[var(--vigil-muted)]">
                  <th className="px-4 py-2 font-medium">{t("registre.colTime")}</th>
                  <th className="px-3 py-2 font-medium">{t("registre.colName")}</th>
                  <th className="px-3 py-2 font-medium">{t("registre.colGranularity")}</th>
                  <th className="px-3 py-2 font-medium">{t("registre.colSize")}</th>
                  <th className="px-3 py-2 font-medium">{t("registre.colLink")}</th>
                  <th className="px-3 py-2 text-right font-medium">{t("registre.colActions")}</th>
                </tr>
              </thead>
              <tbody>
                {reports.map((report) => {
                  const link = linkFor(report);
                  return (
                    <tr key={report.name} className="border-b border-[var(--vigil-border)] last:border-b-0">
                      <td className="px-4 py-2 text-[11px] text-[var(--vigil-muted)]">{stamp(report.mtime)}</td>
                      <td className="px-3 py-2 font-mono text-[11px] text-[var(--vigil-text)]">{report.name}</td>
                      <td className="px-3 py-2 text-[11px] text-[var(--vigil-muted)]">
                        {report.granularity === "daily"
                          ? t("registre.granularityDaily")
                          : report.granularity === "weekly"
                            ? t("registre.granularityWeekly")
                            : report.granularity === "range"
                              ? t("registre.granularityRange")
                              : t("registre.granularityUnknown")}
                      </td>
                      <td className="px-3 py-2 text-[11px] text-[var(--vigil-muted)]">{formatBytes(report.size)}</td>
                      <td className="px-3 py-2">
                        {link ? (
                          <a
                            href={link}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex items-center gap-1 text-[11px] text-[var(--vigil-primary)] hover:underline"
                          >
                            BookStack
                            <ExternalLink className="size-3" />
                          </a>
                        ) : (
                          <span className="text-[11px] text-[var(--vigil-muted)]">{t("registre.noLink")}</span>
                        )}
                      </td>
                      <td className="px-3 py-2">
                        <div className="flex items-center justify-end gap-1">
                          <button
                            type="button"
                            onClick={() => void openPreview(report)}
                            className="rounded-md border border-[var(--vigil-border)] px-2 py-1 text-[11px] text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)]"
                          >
                            {t("registre.preview")}
                          </button>
                          <button
                            type="button"
                            onClick={() => copyPath(report)}
                            title={report.path || report.name}
                            className="inline-flex items-center gap-1 rounded-md border border-[var(--vigil-border)] px-2 py-1 text-[11px] text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)]"
                          >
                            <ClipboardCopy className="size-3" />
                            {copied === report.name ? t("registre.copied") : t("registre.copyPath")}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {preview ? (
        <div className="fixed inset-0 z-[90] flex justify-end" role="dialog" aria-label={t("registre.previewTitle", { name: preview.name })}>
          <div className="absolute inset-0 bg-black/40" aria-hidden="true" onClick={() => setPreview(null)} />
          <aside className="relative flex h-full w-full max-w-3xl flex-col border-l border-[var(--vigil-border)] bg-[var(--vigil-card)] shadow-[-8px_0_30px_rgba(0,0,0,0.18)]">
            <div className="flex items-center gap-2 border-b border-[var(--vigil-border)] px-4 py-3">
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-[var(--vigil-text)]">{preview.name}</span>
              <button
                type="button"
                aria-label={t("registre.close")}
                onClick={() => setPreview(null)}
                className="inline-flex size-7 items-center justify-center rounded-md text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]"
              >
                <X className="size-4" />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-4 py-3 text-xs leading-relaxed">
              {previewLoading ? (
                <div className="flex items-center gap-2 text-[var(--vigil-muted)]">
                  <Loader2 className="size-4 animate-spin" />
                  {t("registre.previewLoading")}
                </div>
              ) : previewError ? (
                <div className="rounded-md border border-[var(--vigil-error)] bg-[var(--vigil-error-bg)] px-3 py-2 text-[var(--vigil-error)]">
                  {previewError}
                </div>
              ) : (
                <Markdown text={preview.content} />
              )}
            </div>
          </aside>
        </div>
      ) : null}
    </div>
  );
}

function PipelineRow({
  rowKey,
  label,
  job,
  paused,
  editing,
  onEdit,
  onTogglePause,
  onEditChange,
  onEditSave,
  onEditCancel,
}: {
  rowKey: string;
  label: string;
  job: CronJob;
  paused: boolean;
  editing: EditState | null;
  onEdit: (mode: EditState["mode"]) => void;
  onTogglePause: () => void;
  onEditChange: (value: string) => void;
  onEditSave: () => void;
  onEditCancel: () => void;
}) {
  const { t } = useTranslation();
  const window = windowArg(job.script_args);
  return (
    <>
      <tr className="border-b border-[var(--vigil-border)] align-middle last:border-b-0" data-testid={`pipeline-row-${rowKey}`}>
        <td className="px-4 py-2">
          <div className="flex items-center gap-1.5">
            <span className="rounded border border-[var(--vigil-border)] px-1.5 py-px text-[10px] text-[var(--vigil-muted)]">
              {label}
            </span>
            <span className="truncate font-medium text-[var(--vigil-text)]">{job.name || job.id}</span>
          </div>
        </td>
        <td className="px-3 py-2">
          <code className="rounded bg-[var(--vigil-muted-bg)] px-1.5 py-0.5 font-mono text-[11px]">
            {job.schedule_display || job.schedule?.display || "—"}
          </code>
        </td>
        <td className="px-3 py-2 font-mono text-[11px] text-[var(--vigil-text)]">{window || "—"}</td>
        <td className="px-3 py-2 text-[11px]">
          {paused ? (
            <span className="text-[var(--vigil-muted)]">{t("schedules.paused")}</span>
          ) : job.next_run_at ? (
            <time dateTime={job.next_run_at}>{stamp(job.next_run_at)}</time>
          ) : (
            <span className="text-[var(--vigil-muted)]">—</span>
          )}
        </td>
        <td className="px-3 py-2 text-[11px]">
          {job.last_status ? (
            <span className={cn(job.last_status === "ok" ? "text-[var(--vigil-ok)]" : "text-[var(--vigil-error)]")}>
              {job.last_status}
            </span>
          ) : (
            <span className="text-[var(--vigil-muted)]">{t("registre.neverRun")}</span>
          )}
          {job.last_run_at ? (
            <span className="ml-1 text-[10px] text-[var(--vigil-muted)]">{stamp(job.last_run_at)}</span>
          ) : null}
        </td>
        <td className="px-3 py-2">
          <div className="flex items-center justify-end gap-1">
            <button
              type="button"
              onClick={() => onEdit("schedule")}
              className="rounded-md border border-[var(--vigil-border)] px-2 py-1 text-[11px] text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)]"
            >
              {t("registre.editSchedule")}
            </button>
            <button
              type="button"
              onClick={() => onEdit("window")}
              className="rounded-md border border-[var(--vigil-border)] px-2 py-1 text-[11px] text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)]"
            >
              {t("registre.editWindow")}
            </button>
            <button
              type="button"
              title={paused ? t("registre.resume") : t("registre.pause")}
              aria-label={paused ? t("registre.resume") : t("registre.pause")}
              onClick={onTogglePause}
              className="inline-flex size-7 items-center justify-center rounded-md border border-[var(--vigil-border)] text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)] hover:text-[var(--vigil-text)]"
            >
              {paused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
            </button>
          </div>
        </td>
      </tr>
      {editing ? (
        <tr className="border-b border-[var(--vigil-border)] bg-[var(--vigil-muted-bg)]">
          <td colSpan={6} className="px-4 py-3">
            <div className="space-y-2" data-testid={`registre-edit-${editing.mode}`}>
              <input
                aria-label={editing.mode === "window" ? t("registre.windowLabel") : t("schedules.scheduleLabel")}
                value={editing.value}
                placeholder={editing.mode === "window" ? "7d" : "every 5m / 0 18 * * 5 / 2026-11-01T09:00"}
                onChange={(event) => onEditChange(event.target.value)}
                className="w-full max-w-md rounded-md border border-[var(--vigil-border)] bg-[var(--vigil-bg)] px-2 py-1.5 font-mono text-xs text-[var(--vigil-text)]"
              />
              <p className="max-w-2xl text-[10px] text-[var(--vigil-muted)]">
                {editing.mode === "window" ? t("registre.windowHint") : t("registre.scheduleHint")}
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
                  onClick={onEditSave}
                  className="inline-flex items-center gap-1.5 rounded-md bg-[var(--vigil-primary)] px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
                >
                  {editing.saving ? <Loader2 className="size-3.5 animate-spin" /> : null}
                  {editing.saving ? t("registre.saving") : t("registre.save")}
                </button>
                <button
                  type="button"
                  onClick={onEditCancel}
                  className="rounded-md border border-[var(--vigil-border)] px-3 py-1.5 text-xs text-[var(--vigil-muted)] hover:bg-[var(--vigil-card)]"
                >
                  {t("registre.cancel")}
                </button>
              </div>
            </div>
          </td>
        </tr>
      ) : null}
    </>
  );
}
