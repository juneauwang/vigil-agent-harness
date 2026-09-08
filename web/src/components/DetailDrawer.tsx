import { useTranslation } from "react-i18next";
import "@/i18n";
import { FilePenLine, X } from "lucide-react";
import { DetailTree } from "@/components/DetailTree";
import { EnvBadge, StatusPill } from "@/components/StatusBits";
import { kindLabel, type GraphEntityRef } from "@/lib/topologyGraph";
import { cn } from "@/lib/ops";

/**
 * Detail drawer (batch 35): slides in from the right, replacing in-card
 * expansion (DetailBox). Content = header (entity name + EnvBadge +
 * StatusPill) + full-width DetailTree; long commands don't wrap (DetailTree
 * strings use whitespace-pre + horizontal scroll); overlay click closes.
 * Reuses the CSS variables and the existing dropdown/approval-modal shadow
 * levels.
 */
export default function DetailDrawer({
  entity,
  onClose,
  onEdit,
}: {
  entity: GraphEntityRef | null;
  onClose: () => void;
  /** task29 PART C：『编辑 YAML 源文件』入口（图节点详情与卡片/列表共用同一
   *  编辑抽屉流程）；不传则不渲染按钮（如 Overview 页未接线时）。 */
  onEdit?: (entity: GraphEntityRef) => void;
}) {
  const { t } = useTranslation();
  if (!entity) return null;
  return (
    <div className="fixed inset-0 z-[90] flex justify-end" role="dialog" aria-label={t("topology.drawerAria", { name: entity.name })}>
      <div className="absolute inset-0 bg-black/50" onClick={onClose} aria-label={t("topology.maskCloseAria")} />
      <aside className="relative flex h-full w-full max-w-xl flex-col border-l border-[var(--vigil-border)] bg-[var(--vigil-card)] shadow-[-8px_0_30px_rgba(0,0,0,0.18)]">
        <div className="flex flex-wrap items-center gap-2 border-b border-[var(--vigil-border)] p-3.5">
          <span className="rounded bg-[var(--vigil-muted-bg)] px-1.5 py-px text-[10px] font-medium uppercase tracking-wide text-[var(--vigil-muted)]">
            {kindLabel(entity.kind)}
          </span>
          <span className="text-sm font-semibold text-[var(--vigil-text)]">{entity.card.name}</span>
          <EnvBadge env={entity.card.env} />
          <StatusPill status={entity.card.status} />
          {onEdit ? (
            <button
              type="button"
              onClick={() => onEdit(entity)}
              data-testid="drawer-edit-yaml"
              aria-label={t("yamlEditor.editAria")}
              title={t("yamlEditor.editAria")}
              className="vigil-btn ml-auto h-7 px-2 text-xs"
            >
              <FilePenLine className="size-3.5" />
              {t("yamlEditor.editAria")}
            </button>
          ) : null}
          <button
            type="button"
            onClick={onClose}
            aria-label={t("topology.closeAria")}
            className={cn(
              "flex size-7 items-center justify-center rounded-md text-[var(--vigil-muted)] hover:bg-[var(--vigil-muted-bg)]",
              onEdit ? "" : "ml-auto",
            )}
          >
            <X className="size-4" />
          </button>
        </div>
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto p-3.5">
          <DetailTree data={entity.detail ?? {}} />
        </div>
      </aside>
    </div>
  );
}
