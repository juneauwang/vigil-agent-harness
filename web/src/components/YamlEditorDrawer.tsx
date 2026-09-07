import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import "@/i18n";
import CodeMirror from "@uiw/react-codemirror";
import { yaml } from "@codemirror/lang-yaml";
import { oneDark } from "@codemirror/theme-one-dark";
import { EditorView } from "@codemirror/view";
import { AlertTriangle, FilePenLine, Loader2, Save, X } from "lucide-react";
import { ApiError, type YamlSaveWarning, type YamlValidationIssue } from "@/lib/api";
import { cn } from "@/lib/ops";

/**
 * YAML 编辑抽屉（task27 PART A）：右侧滑入 + CodeMirror 6（yaml 语法高亮，
 * oneDark 深色主题叠应用色板变量）。RAW round-trip——文本原样 GET/PUT，
 * 服务端跑既有校验器；校验失败 422 的结构化错误带行号内联展示；保存成功
 * 但服务端返回 warnings（如 schedule/alert_auto_run 自动执行豁免因内容哈希
 * 漂移失效）时以警示条明示。脏状态关闭需确认。人工是作者——保存不弹审批。
 */

export interface YamlEditorTarget {
  /** 抽屉标题（文件名 / 实体名）。 */
  title: string;
  /** 副标题（事实源文件，如 runbooks/harbor-restart.yaml）。 */
  subtitle?: string;
  load: () => Promise<string>;
  save: (text: string) => Promise<{ warnings?: YamlSaveWarning[] }>;
}

/** 应用色板上的 CodeMirror 主题（跟随 --vigil CSS 变量，不用默认浅色）。 */
const vigilEditorTheme = EditorView.theme({
  "&": { backgroundColor: "var(--vigil-terminal-bg)", color: "#cbd5e1" },
  ".cm-gutters": {
    backgroundColor: "var(--vigil-terminal-bg)",
    color: "var(--vigil-muted)",
    border: "none",
  },
  ".cm-activeLine": { backgroundColor: "rgba(148, 163, 184, 0.08)" },
  ".cm-activeLineGutter": { backgroundColor: "rgba(148, 163, 184, 0.12)" },
  "&.cm-focused": { outline: "none" },
});

export default function YamlEditorDrawer({
  target,
  onClose,
  onSaved,
}: {
  target: YamlEditorTarget | null;
  onClose: () => void;
  /** 保存成功后回调（页面刷新数据；warning 存在时抽屉保持打开显示告警）。 */
  onSaved?: (warnings: YamlSaveWarning[]) => void;
}) {
  const { t } = useTranslation();
  const [original, setOriginal] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<YamlValidationIssue[] | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<YamlSaveWarning[] | null>(null);
  const targetRef = useRef<YamlEditorTarget | null>(target);

  // target 变化（打开新实体）→ 重置状态并加载原文。
  useEffect(() => {
    if (!target) return;
    targetRef.current = target;
    setOriginal(null);
    setText("");
    setLoadError(null);
    setErrors(null);
    setSaveError(null);
    setWarnings(null);
    let alive = true;
    target
      .load()
      .then((content) => {
        if (!alive) return;
        setOriginal(content);
        setText(content);
      })
      .catch((e: unknown) => {
        if (alive) setLoadError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, [target]);

  const dirty = original !== null && text !== original;

  const close = useCallback(() => {
    if (dirty && !window.confirm(t("yamlEditor.closeConfirm"))) return;
    onClose();
  }, [dirty, onClose, t]);

  // Esc 关闭（脏状态同样走确认）。
  useEffect(() => {
    if (!target) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [target, close]);

  const extensions = useMemo(() => [yaml(), vigilEditorTheme], []);

  const handleSave = async () => {
    const current = targetRef.current;
    if (!current || saving) return;
    setSaving(true);
    setErrors(null);
    setSaveError(null);
    setWarnings(null);
    try {
      const resp = await current.save(text);
      const respWarnings = resp.warnings ?? [];
      if (respWarnings.length > 0) {
        // 成功但带告警（自动执行豁免失效）→ 保持打开明示，用户手动关闭。
        setWarnings(respWarnings);
        setOriginal(text);
      } else {
        onSaved?.(respWarnings);
        onClose();
      }
    } catch (e: unknown) {
      if (e instanceof ApiError && Array.isArray(e.details?.errors)) {
        setErrors(e.details.errors as YamlValidationIssue[]);
      } else {
        setSaveError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setSaving(false);
    }
  };

  if (!target) return null;

  return (
    <div className="fixed inset-0 z-[90] flex justify-end" role="dialog" aria-label={t("yamlEditor.drawerAria", { name: target.title })} data-testid="yaml-editor-drawer">
      <div className="absolute inset-0 bg-black/50" onClick={close} aria-label={t("yamlEditor.maskCloseAria")} />
      <aside className="relative flex h-full w-full max-w-2xl flex-col border-l border-[var(--vigil-border)] bg-[var(--vigil-card)] shadow-[-8px_0_30px_rgba(0,0,0,0.18)]">
        {/* Header: 文件名 + 关闭 */}
        <div className="flex flex-wrap items-center gap-2 border-b border-[var(--vigil-border)] p-3.5">
          <FilePenLine className="size-4 text-[var(--vigil-muted)]" />
          <span className="text-sm font-semibold text-[var(--vigil-text)]">{target.title}</span>
          {target.subtitle ? (
            <span className="truncate font-mono text-[10px] text-[var(--vigil-muted)]" title={target.subtitle}>
              {target.subtitle}
            </span>
          ) : null}
          <button
            type="button"
            onClick={close}
            aria-label={t("yamlEditor.closeAria")}
            className="ml-auto flex size-7 items-center justify-center rounded-md text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]"
          >
            <X className="size-4" />
          </button>
        </div>

        {/* 校验错误（422，带行号） */}
        {errors && errors.length > 0 ? (
          <div className="border-b border-red-500/30 bg-red-500/10 p-3 text-xs" data-testid="yaml-editor-errors">
            <div className="mb-1.5 flex items-center gap-1.5 font-semibold text-red-600 dark:text-red-400">
              <AlertTriangle className="size-3.5" /> {t("yamlEditor.validationFailed")}
            </div>
            <ul className="space-y-1">
              {errors.map((err, i) => (
                <li key={i} className="flex gap-2 text-[var(--vigil-text)] opacity-90">
                  <span className="shrink-0 rounded bg-[var(--vigil-muted-bg)] px-1.5 py-px font-mono text-[10px] text-red-600 dark:text-red-400">
                    {t("yamlEditor.lineRef", { line: err.line })}
                    {err.column ? `:${err.column}` : ""}
                  </span>
                  <span className="min-w-0 flex-1 break-words">{err.message}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {/* 保存成功但自动执行授权失效（成功 + 警示） */}
        {warnings && warnings.length > 0 ? (
          <div className="border-b border-amber-500/30 bg-amber-500/10 p-3 text-xs" data-testid="yaml-editor-warnings">
            <div className="mb-1.5 flex items-center gap-1.5 font-semibold text-amber-600 dark:text-amber-400">
              <AlertTriangle className="size-3.5" /> {t("yamlEditor.savedWithWarnings")}
            </div>
            <ul className="space-y-1">
              {warnings.map((w, i) => (
                <li key={i} className="break-words text-[var(--vigil-text)] opacity-90">{w.message}</li>
              ))}
            </ul>
            <div className="mt-2 flex justify-end">
              <button
                type="button"
                onClick={onClose}
                data-testid="yaml-editor-warnings-close"
                className="vigil-btn border border-[var(--vigil-border)] px-3 py-1 text-xs"
              >
                {t("common.close")}
              </button>
            </div>
          </div>
        ) : null}

        {/* Editor */}
        <div className="min-h-0 flex-1 overflow-hidden">
          {loadError ? (
            <div className="p-6 text-sm text-[var(--vigil-error)]">{loadError}</div>
          ) : original === null ? (
            <div className="flex items-center justify-center gap-2 py-16 text-sm text-[var(--vigil-muted)]">
              <Loader2 className="size-4 animate-spin" /> {t("yamlEditor.loading")}
            </div>
          ) : (
            <CodeMirror
              value={text}
              height="100%"
              style={{ height: "100%" }}
              theme={oneDark}
              extensions={extensions}
              basicSetup={{ foldGutter: true, highlightActiveLine: true }}
              onChange={(value: string) => setText(value)}
              aria-label={t("yamlEditor.editorAria")}
            />
          )}
        </div>

        {/* Footer: cancel/save */}
        <div className="flex items-center gap-2 border-t border-[var(--vigil-border)] p-3.5">
          {saveError ? (
            <span className="min-w-0 flex-1 truncate text-xs text-[var(--vigil-error)]" title={saveError}>
              {saveError}
            </span>
          ) : (
            <span className="min-w-0 flex-1 text-xs text-[var(--vigil-muted)]">
              {dirty ? t("yamlEditor.dirtyHint") : ""}
            </span>
          )}
          <button
            type="button"
            onClick={close}
            disabled={saving}
            className={cn("vigil-btn border border-[var(--vigil-border)] px-3 py-1.5 text-xs")}
          >
            {t("yamlEditor.cancel")}
          </button>
          <button
            type="button"
            onClick={() => void handleSave()}
            disabled={saving || original === null}
            data-testid="yaml-editor-save"
            className="vigil-btn px-3 py-1.5 text-xs"
          >
            {saving ? <Loader2 className="mr-1 inline size-3.5 animate-spin" /> : <Save className="mr-1 inline size-3.5" />}
            {t("yamlEditor.save")}
          </button>
        </div>
      </aside>
    </div>
  );
}
