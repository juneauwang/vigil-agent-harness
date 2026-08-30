import { useEffect, useMemo, useState } from "react";
import { Grid3x3, ShieldCheck, TriangleAlert, Wand2, X } from "lucide-react";
import { api } from "@/lib/api";
import type { MatrixData, MatrixLevel } from "@/lib/api";
import { EmptyState } from "@/components/EmptyState";
import { cn } from "@/lib/ops";

/**
 * 操作矩阵页（YAPL P3 §11.7 定案）。
 *
 * 行 = 动作枚举（schemas.yaml actions 23 个），列 = 环境（matrix.yaml 键），
 * 格 = 三态下拉（execute / approve / {approve: required}）+ 每格来源
 * （模板名 or 手动改）。顶部说明「矩阵是人工安全资产，修改即审计」。
 *
 * 修改走 PUT /api/matrix（人工通道，后端落审计）；LLM 只有 matrix_query
 * 只读工具，本页无任何 LLM set 路径。空态引导用四模板 init（模板 4 =
 * 三步级联多选 modal）。
 */

const LEVEL_OPTIONS: Array<{ value: MatrixLevel; label: string; title: string }> = [
  { value: "execute", label: "execute", title: "直接执行（低风险）" },
  { value: "approve", label: "approve", title: "交互执行需人工审批" },
  { value: "required", label: "required", title: "{approve: required} 强制人工（不 smart）" },
];

const LEVEL_COLORS: Record<MatrixLevel, string> = {
  execute: "text-[var(--vigil-ok)]",
  approve: "text-[var(--vigil-warn)]",
  required: "text-[var(--vigil-error)]",
};

const TEMPLATES: Array<{ id: string; label: string; desc: string }> = [
  { id: "template1", label: "模板 1 · 单人", desc: "local，execute 全部，仅高危 4 需审批" },
  { id: "template2", label: "模板 2 · 小团队", desc: "local / test / dev / prod 严格度阶梯" },
  { id: "template3", label: "模板 3 · 中型", desc: "local / test / uat / dev / prod，prod 最高限制" },
  { id: "template4", label: "模板 4 · 自定义", desc: "三步级联多选（execute → approve → 其余 required）" },
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
        title={LEVEL_OPTIONS.find((o) => o.value === level)?.title}
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
        title={source === "manual" ? "手动改过（人工通道）" : `来源模板：${source}`}
      >
        {source === "manual" ? "· 手动改" : source || "-"}
      </span>
    </div>
  );
}

export default function MatrixPage() {
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
        else setError(resp.error ?? "矩阵加载失败");
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
    // 乐观更新
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
          flash(`已更新 ${env}.${action} → ${level}（修改即审计）`);
        } else {
          setError(resp.error ? String(resp.error) : "更新失败");
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
          flash(`已生成矩阵（${template}，热生效无需重启）`);
        } else {
          setInitError(resp.error ?? "生成失败");
        }
      })
      .catch((e: unknown) => setInitError(e instanceof Error ? e.message : String(e)));
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <Grid3x3 className="size-5 text-[var(--vigil-muted)]" />
          <h1 className="text-lg font-semibold">操作矩阵</h1>
        </div>
        <span className="ml-auto flex items-center gap-1 text-xs text-[var(--vigil-muted)]">
          <ShieldCheck className="size-3.5 text-[var(--vigil-ok)]" />
          矩阵是人工安全资产，修改即审计（LLM 只有 matrix_query 只读）
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

      {loading && <div className="py-8 text-center text-xs text-[var(--vigil-muted)]">加载中…</div>}

      {!loading && isEmpty && (
        <EmptyState
          icon={<Grid3x3 className="size-8" />}
          title="尚未生成操作矩阵"
          description="矩阵是权限唯一裁决（runbook 每步执行时查）。用 setup 四模板生成后热生效，无需重启服务。"
          hint="已存在矩阵时本页直接显示；重建请用 vigil matrix reset（防误覆盖）。"
          action={
            <div className="flex flex-wrap items-center justify-center gap-2">
              {TEMPLATES.map((t) => (
                <button
                  key={t.id}
                  className="rounded border border-[var(--vigil-border)] bg-[var(--vigil-card)] px-3 py-2 text-left hover:border-[var(--vigil-primary)]"
                  onClick={() => (t.id === "template4" ? setCascadeOpen(true) : initTemplate(t.id))}
                >
                  <div className="text-xs font-medium">{t.label}</div>
                  <div className="text-[10px] text-[var(--vigil-muted)]">{t.desc}</div>
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
              <div className="mb-1 font-medium text-[var(--vigil-warn)]">漏配警告（默认 approve 保守）</div>
              {(data.warnings ?? []).map((w, i) => (
                <div key={i}>· {w}</div>
              ))}
            </div>
          )}
          <div className="mb-3 flex flex-wrap items-center gap-3 text-[11px] text-[var(--vigil-muted)]">
            <span>
              顶层来源：<b>{data.source || "未生成"}</b>
            </span>
            {data.base_template && <span>基底模板：{data.base_template}</span>}
            {data.updated_at && <span>更新：{data.updated_at}</span>}
            <span className="ml-auto flex items-center gap-3">
              {LEVEL_OPTIONS.map((o) => (
                <span key={o.value} className="flex items-center gap-1">
                  <span className={cn("font-medium", LEVEL_COLORS[o.value])}>{o.label}</span>
                  <span className="opacity-70">{o.title}</span>
                </span>
              ))}
            </span>
          </div>
          <div className="min-h-0 flex-1 overflow-auto rounded-md border border-[var(--vigil-border)]">
            <table className="w-full border-collapse text-xs">
              <thead className="sticky top-0 bg-[var(--vigil-muted-bg)]">
                <tr>
                  <th className="border-b border-r border-[var(--vigil-border)] px-2 py-2 text-left font-medium">
                    动作
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
            · 手动改过的格子来源显示「· 手动改」；· 每格改动走 PUT /api/matrix，审计事件在「审计」页可查（type=matrix_change）
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
 * 模板 4 三步级联多选（§11.3）：先选 execute 集 → 从剩余选 approve 集 →
 * 其余自动 {approve: required}。已选的从后续选项移除（级联语义）。
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
          <h2 className="text-sm font-semibold">模板 4 · 自定义级联（{step === 1 ? "第 1 步 / 共 3 步" : "第 2 步 / 共 3 步"}）</h2>
          <button className="ml-auto rounded p-1 hover:bg-[var(--vigil-muted-bg)]" onClick={onCancel} aria-label="关闭">
            <X className="size-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-4">
          <div className="mb-3 text-xs text-[var(--vigil-muted)]">
            {step === 1
              ? "选择 execute 集（直接执行，低风险）——已选的会从后续 approve 选项中移除。"
              : "从剩余动作选择 approve 集（交互执行需审批）——其余自动为 {approve: required}（强制人工）。"}
          </div>
          <div className="mb-2 flex items-center gap-2 text-xs font-medium">
            {step === 1 ? "execute 集" : "approve 集"}
            <span className="text-[var(--vigil-muted)]">（{step === 1 ? executeSet.length : approveSet.length} 已选）</span>
            {step === 1 && (
              <button
                className="ml-auto rounded border border-[var(--vigil-border)] px-2 py-0.5 text-[10px] hover:border-[var(--vigil-primary)]"
                onClick={() => setExecuteSet(executeSet.length === actions.length ? [] : [...actions])}
              >
                {executeSet.length === actions.length ? "清空" : "全选"}
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
              <span className="font-medium text-[var(--vigil-error)]">required（强制人工）</span>
              <span className="ml-1 text-[var(--vigil-muted)]">
                {requiredSet.length} 个：{requiredSet.join(", ") || "（无）"}
              </span>
            </div>
          )}
        </div>
        <div className="flex items-center justify-between gap-2 border-t border-[var(--vigil-border)] px-4 py-3">
          <button className="rounded px-3 py-1.5 text-xs hover:bg-[var(--vigil-muted-bg)]" onClick={onCancel}>
            取消
          </button>
          <div className="flex items-center gap-2">
            {step === 2 && (
              <button
                className="rounded border border-[var(--vigil-border)] px-3 py-1.5 text-xs hover:border-[var(--vigil-primary)]"
                onClick={() => setStep(1)}
              >
                上一步
              </button>
            )}
            {step === 1 ? (
              <button
                className="rounded bg-[var(--vigil-primary)] px-3 py-1.5 text-xs text-white disabled:opacity-50"
                disabled={executeSet.length === 0}
                onClick={() => setStep(2)}
              >
                下一步
              </button>
            ) : (
              <button
                className="rounded bg-[var(--vigil-primary)] px-3 py-1.5 text-xs text-white"
                onClick={() => onConfirm({ execute: executeSet, approve: approveSet })}
              >
                生成矩阵
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
