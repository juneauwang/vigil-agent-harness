import { useTranslation } from "react-i18next";
import "@/i18n";
import { Square } from "lucide-react";

/**
 * Chat-page "stop" button (batch 36): shown while the agent is busy; clicking
 * issues a real backend interrupt. Warning styling (red family, next to
 * "send"); disabled with "stopping…" while the stop request is in flight.
 * Extracted as its own component for easy node-env SSR unit tests (visibility).
 */
export default function StopButton({
  stopping,
  onStop,
}: {
  stopping: boolean;
  onStop: () => void;
}) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={onStop}
      disabled={stopping}
      aria-label={t("chat.stopAria")}
      title={t("chat.stopTitle")}
      className="vigil-btn h-8 shrink-0 whitespace-nowrap border border-red-500/50 px-3 text-sm text-red-600 hover:bg-red-500/10 disabled:opacity-50 dark:text-red-400"
    >
      <Square className="size-3.5" /> {stopping ? t("chat.stopping") : t("chat.stop")}
    </button>
  );
}
