import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import "@/i18n";
import { translateBackendMessage } from "@/lib/backendMsg";
import { Grid3x3, ShieldCheck, TriangleAlert, Wand2, X } from "lucide-react";
import { api } from "@/lib/api";
import type { MatrixData, MatrixLevel } from "@/lib/api";
import { EmptyState } from "@/components/EmptyState";
import { cn } from "@/lib/ops";

/**
 * Ops matrix page (YAPL P3 §11.7 final).
 *
 * Rows = action enum (23 actions in schemas.yaml), columns = environments
 * (matrix.yaml keys), cells = three-state dropdown (execute / approve /
 * {approve: required}) + per-cell source (template name or manual change).
 * Header note: "the matrix is a manually-owned safety asset; every change is
 * audited".
 *
 * Changes go through PUT /api/matrix (manual channel, backend writes the
 * audit); the LLM only has the read-only matrix_query tool — this page has no
 * LLM set path. Empty state guides via the four-template init (template 4 =
 * three-step cascade multi-select modal).
 */

const LEVEL_OPTIONS: Array<{ value: MatrixLevel; label: string; titleKey: string }> = [
  { value: "execute", label: "execute", titleKey: "matrix.levelTitle.execute" },
  { value: "approve", label: "approve", titleKey: "matrix.levelTitle.approve" },
  { value: "required", label: "required", titleKey: "matrix.levelTitle.required" },
];

const LEVEL_COLORS: Record<MatrixLevel, string> = {
  execute: "text-[var(--vigil-ok)]",
  approve: "text-[var(--vigil-warn)]",
  required: "text-[var(--vigil-error)]",
};

const TEMPLATES: Array<{ id: string; labelKey: string; descKey: string }> = [
  { id: "template1", labelKey: "matrix.templates.template1_label", descKey: "matrix.templates.template1_desc" },
  { id: "template2", labelKey: "matrix.templates.template2_label", descKey: "matrix.templates.template2_desc" },
  { id: "template3", labelKey: "matrix.templates.template3_label", descKey: "matrix.templates.template3_desc" },
  { id: "template4", labelKey: "matrix.templates.template4_label", descKey: "matrix.templates.template4_desc" },
];

function LevelCell({
  level,
  source,
  onChange,
  busy,
}: {
  level: MatrixLevel;
  source: string;
  onChange: (level: MatrixLevel) => void;
  busy: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col items-start gap-1">
      <select
        className={cn(
          "w-full cursor-pointer rounded border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-1.5 py-1 text-xs",
          "focus:border-[var(--vigil-primary)] focus:outline-none disabled:opacity-50",
          LEVEL_COLORS[level],
        )}
        value={level}
        disabled={busy}
        onChange={(e) => onChange(e.target.value as MatrixLevel)}
        title={t(LEVEL_OPTIONS.find((o) => o.value === level)?.titleKey ?? "")}
      >
        {LEVEL_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <span
        className={cn(
          "text-[10px] leading-none",
          source === "manual" ? "text-[var(--vigil-warn)]" : "text-[var(--vigil-muted)]",
        )}
        title={source === "manual" ? t("matrix.cellManualTitle") : t("matrix.cellSourceTitle", { source })}
      >
        {source === "manual" ? t("matrix.cellManual") : source || "-"}
      </span>
    </div>
  );
}

export default function MatrixPage() {
  const { t, i18n } = useTranslation();
  const blang = i18n.language === "en" ? "en" : "zh";
  const [data, setData] = useState<MatrixData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<Record<string, boolean>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [initError, setInitError] = useState<string | null>(null);
  const [cascadeOpen, setCascadeOpen] = useState(false);

  const reload = () => {
    setLoading(true);
    setError(null);
    api
      .getMatrix()
      .then((resp) => {
        if (resp.ok && resp.data) setData(resp.data);
        else setError(resp.error ?? t("matrix.loadFailed"));
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const envs = useMemo(() => Object.keys(data?.matrix ?? {}), [data]);
  const actions = useMemo(() => data?.actions ?? [], [data]);
  const isEmpty = !!data && envs.length === 0;

  const flash = (msg: string) => {
    setNotice(msg);
    window.setTimeout(() => setNotice(null), 4000);
  };

  const changeCell = (env: string, action: string, level: MatrixLevel) => {
    const old = data?.matrix?.[env]?.[action] ?? "approve";
    if (old === level) return;
    // Optimistic update
    setData((prev) => {
      if (!prev) return prev;
      const matrix = { ...prev.matrix, [env]: { ...(prev.matrix[env] ?? {}), [action]: level } };
      const sources = { ...prev.sources, [env]: { ...(prev.sources[env] ?? {}), [action]: "manual" } };
      return { ...prev, matrix, sources, source: "manual" };
    });
    const cellKey = env + ":" + action;
    setSaving((prev) => ({ ...prev, [cellKey]: true }));
    api
      .setMatrixCell(env, action, level)
      .then((resp) => {
        if (resp.ok) {
          flash(t("matrix.updatedMsg", { env, action, level }));
        } else {
          setError(resp.error ? String(resp.error) : t("matrix.updateFailed"));
          reload();
        }
      })
      .catch((e: unknown) => {
        setError(e instanceof Error ? e.message : String(e));
        reload();
      })
      .finally(() => {
        setSaving((prev) => {
          const next = { ...prev };
          delete next[cellKey];
          return next;
        });
      });
  };

  const initTemplate = (template: string, selections?: { execute: string[]; approve: string[] }) => {
    setInitError(null);
    api
      .initMatrix(template, selections)
      .then((resp) => {
        if (resp.ok && resp.data) {
          setData(resp.data);
          flash(t("matrix.generatedMsg", { template }));
        } else {
          setInitError(resp.error ?? t("matrix.initFailed"));
        }
      })
      .catch((e: unknown) => setInitError(e instanceof Error ? e.message : String(e)));
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <Grid3x3 className="size-5 text-[var(--vigil-muted)]" />
          <h1 className="text-lg font-semibold">{t("matrix.title")}</h1>
        </div>
        <span className="ml-auto flex items-center gap-1 text-xs text-[var(--vigil-muted)]">
          <ShieldCheck className="size-3.5 text-[var(--vigil-ok)]" />
          {t("matrix.headerNote")}
        </span>
      </div>

      {notice && (
        <div className="mb-3 rounded border border-[var(--vigil-ok)]/40 bg-[var(--vigil-ok)]/10 px-3 py-2 text-xs text-[var(--vigil-text)]">
          {notice}
        </div>
      )}
      {error && (
        <div className="mb-3 flex items-center gap-2 rounded border border-[var(--vigil-error)]/40 bg-[var(--vigil-error)]/10 px-3 py-2 text-xs text-[var(--vigil-error)]">
          <TriangleAlert className="size-4 shrink-0" />
          {error}
        </div>
      )}

      {loading && <div className="py-8 text-center text-xs text-[var(--vigil-muted)]">{t("common.loading")}</div>}

      {!loading && isEmpty && (
        <EmptyState
          icon={<Grid3x3 className="size-8" />}
          title={t("matrix.emptyTitle")}
          description={t("matrix.emptyDesc")}
          hint={t("matrix.emptyHint")}
          action={
            <div className="flex flex-wrap items-center justify-center gap-2">
              {TEMPLATES.map((tpl) => (
                <button
                  key={tpl.id}
                  className="rounded border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-3 py-2 text-left hover:border-[var(--vigil-primary)]"
                  onClick={() => (tpl.id === "template4" ? setCascadeOpen(true) : initTemplate(tpl.id))}
                >
                  <div className="text-xs font-medium">{t(tpl.labelKey)}</div>
                  <div className="text-[10px] text-[var(--vigil-muted)]">{t(tpl.descKey)}</div>
                </button>
              ))}
            </div>
          }
        />
      )}
      {initError && (
        <div className="mt-3 flex items-center gap-2 rounded border border-[var(--vigil-error)]/40 bg-[var(--vigil-error)]/10 px-3 py-2 text-xs text-[var(--vigil-error)]">
          <TriangleAlert className="size-4 shrink-0" />
          {initError}
        </div>
      )}

      {!loading && !isEmpty && data && (
        <>
          {(data.warnings ?? []).length > 0 && (
            <div className="mb-3 rounded border border-[var(--vigil-warn)]/40 bg-[var(--vigil-warn)]/10 px-3 py-2 text-xs text-[var(--vigil-text-secondary)]">
              <div className="mb-1 font-medium text-[var(--vigil-warn)]">{t("matrix.warningsTitle")}</div>
              {(data.warnings ?? []).map((w, i) => (
                <div key={i}>· {translateBackendMessage(w, blang)}</div>
              ))}
            </div>
          )}
          <div className="mb-3 flex flex-wrap items-center gap-3 text-[11px] text-[var(--vigil-muted)]">
            <span>
              {t("matrix.topSource")}<b>{data.source || t("matrix.notGenerated")}</b>
            </span>
            {data.base_template && <span>{t("matrix.baseTemplate", { tpl: data.base_template })}</span>}
            {data.updated_at && <span>{t("matrix.updatedAt", { time: data.updated_at })}</span>}
            <span className="ml-auto flex items-center gap-3">
              {LEVEL_OPTIONS.map((o) => (
                <span key={o.value} className="flex items-center gap-1">
                  <span className={cn("font-medium", LEVEL_COLORS[o.value])}>{o.label}</span>
                  <span className="opacity-70">{t(o.titleKey)}</span>
                </span>
              ))}
            </span>
          </div>
          <div className="min-h-0 flex-1 overflow-auto rounded-md border border-[var(--vigil-border)]">
            <table className="w-full border-collapse text-xs">
              <thead className="sticky top-0 bg-[var(--vigil-muted-bg)]">
                <tr>
                  <th className="border-b border-r border-[var(--vigil-border)] px-2 py-2 text-left font-medium">
                    {t("matrix.thAction")}
                  </th>
                  {envs.map((env) => (
                    <th
                      key={env}
                      className="border-b border-r border-[var(--vigil-border)] px-2 py-2 text-left font-medium last:border-r-0"
                    >
                      <span className={cn("vigil-env", env === "prod" ? "env-prod" : env === "local" ? "env-local" : "env-test")}>
                        {env}
                      </span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {actions.map((action, ri) => (
                  <tr key={action} className={ri % 2 === 1 ? "bg-[var(--vigil-muted-bg)]/40" : ""}>
                    <td className="border-b border-r border-[var(--vigil-border)] px-2 py-1.5 font-mono text-[11px]">
                      {action}
                    </td>
                    {envs.map((env) => {
                      const level = (data.matrix?.[env]?.[action] ?? "approve") as MatrixLevel;
                      const source = data.sources?.[env]?.[action] ?? "";
                      return (
                        <td
                          key={env}
                          className="border-b border-r border-[var(--vigil-border)] px-2 py-1.5 last:border-r-0"
                        >
                          <LevelCell
                            level={level}
                            source={source}
                            busy={!!saving[`${env}:${action}`]}
                            onChange={(lv) => changeCell(env, action, lv)}
                          />
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-2 text-[10px] text-[var(--vigil-muted)]">
            {t("matrix.footnote")}
          </div>
        </>
      )}

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
 * Template 4 three-step cascade multi-select (§11.3): pick the execute set →
 * pick the approve set from the remainder → the rest automatically become
 * {approve: required}. Selected items are removed from later options
 * (cascade semantics).
 */
function CascadeModal({
  actions,
  onCancel,
  onConfirm,
}: {
  actions: string[];
  onCancel: () => void;
  onConfirm: (s: { execute: string[]; approve: string[] }) => void;
}) {
  const { t } = useTranslation();
  const [step, setStep] = useState<1 | 2>(1);
  const [executeSet, setExecuteSet] = useState<string[]>([]);
  const [approveSet, setApproveSet] = useState<string[]>([]);

  const remaining = actions.filter((a) => !executeSet.includes(a));
  const requiredSet = remaining.filter((a) => !approveSet.includes(a));

  const toggle = (list: string[], setList: (v: string[]) => void, action: string) => {
    setList(list.includes(action) ? list.filter((a) => a !== action) : [...list, action]);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-lg border border-[var(--vigil-border)] bg-[var(--vigil-card)] shadow-lg">
        <div className="flex items-center gap-2 border-b border-[var(--vigil-border)] px-4 py-3">
          <Wand2 className="size-4 text-[var(--vigil-primary)]" />
          <h2 className="text-sm font-semibold">{t("matrix.cascadeTitle", { step: step === 1 ? t("matrix.cascadeStep1") : t("matrix.cascadeStep2") })}</h2>
          <button className="ml-auto rounded p-1 hover:bg-[var(--vigil-muted-bg)]" onClick={onCancel} aria-label={t("common.close")}>
            <X className="size-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-4">
          <div className="mb-3 text-xs text-[var(--vigil-muted)]">
            {step === 1
              ? t("matrix.cascadeHint1")
              : t("matrix.cascadeHint2")}
          </div>
          <div className="mb-2 flex items-center gap-2 text-xs font-medium">
            {step === 1 ? t("matrix.executeSet") : t("matrix.approveSet")}
            <span className="text-[var(--vigil-muted)]">{t("matrix.selectedCount", { n: step === 1 ? executeSet.length : approveSet.length })}</span>
            {step === 1 && (
              <button
                className="ml-auto rounded border border-[var(--vigil-border)] px-2 py-0.5 text-[10px] hover:border-[var(--vigil-primary)]"
                onClick={() => setExecuteSet(executeSet.length === actions.length ? [] : [...actions])}
              >
                {executeSet.length === actions.length ? t("matrix.clearAll") : t("matrix.selectAll")}
              </button>
            )}
          </div>
          <div className="grid grid-cols-2 gap-1.5">
            {(step === 1 ? actions : remaining).map((action) => {
              const checked = step === 1 ? executeSet.includes(action) : approveSet.includes(action);
              return (
                <label
                  key={action}
                  className={cn(
                    "flex cursor-pointer items-center gap-2 rounded border px-2 py-1.5 text-xs",
                    checked
                      ? "border-[var(--vigil-primary)] bg-[var(--vigil-accent-bg)]"
                      : "border-[var(--vigil-border)] hover:border-[var(--vigil-primary)]",
                  )}
                >
                  <input
                    type="checkbox"
                    className="size-3.5 accent-[var(--vigil-primary)]"
                    checked={checked}
                    onChange={() => toggle(step === 1 ? executeSet : approveSet, step === 1 ? setExecuteSet : setApproveSet, action)}
                  />
                  <span className="font-mono text-[11px]">{action}</span>
                </label>
              );
            })}
          </div>
          {step === 2 && (
            <div className="mt-3 rounded border border-[var(--vigil-error)]/30 bg-[var(--vigil-error)]/5 px-3 py-2 text-xs">
              <span className="font-medium text-[var(--vigil-error)]">{t("matrix.requiredLabel")}</span>
              <span className="ml-1 text-[var(--vigil-muted)]">
                {t("matrix.requiredCount", { n: requiredSet.length, list: requiredSet.join(", ") || t("matrix.requiredNone") })}
              </span>
            </div>
          )}
        </div>
        <div className="flex items-center justify-between gap-2 border-t border-[var(--vigil-border)] px-4 py-3">
          <button className="rounded px-3 py-1.5 text-xs hover:bg-[var(--vigil-muted-bg)]" onClick={onCancel}>
            {t("common.cancel")}
          </button>
          <div className="flex items-center gap-2">
            {step === 2 && (
              <button
                className="rounded border border-[var(--vigil-border)] px-3 py-1.5 text-xs hover:border-[var(--vigil-primary)]"
                onClick={() => setStep(1)}
              >
                {t("matrix.prevStep")}
              </button>
            )}
            {step === 1 ? (
              <button
                className="rounded bg-[var(--vigil-primary)] px-3 py-1.5 text-xs text-white disabled:opacity-50"
                disabled={executeSet.length === 0}
                onClick={() => setStep(2)}
              >
                {t("matrix.nextStep")}
              </button>
            ) : (
              <button
                className="rounded bg-[var(--vigil-primary)] px-3 py-1.5 text-xs text-white"
                onClick={() => onConfirm({ execute: executeSet, approve: approveSet })}
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
