import { useTranslation } from "react-i18next";
import "@/i18n";
import { envClass, statusPillClass, statusDotClass, statusTone } from "@/lib/ops";
import { cn } from "@/lib/ops";

/** Env badge (prod red / test amber / dev orange / local blue / others gray), hover explains. */
export function EnvBadge({ env }: { env?: string }) {
  const { t } = useTranslation();
  if (!env?.trim()) return null;
  const key = env.trim().toLowerCase();
  const label = ["prod", "test", "dev", "local"].includes(key)
    ? t(`common.env.${key}`)
    : t("common.env.fallback");
  return (
    <span className={cn("vigil-env", envClass(env))} title={label}>
      {env}
    </span>
  );
}

/** Status pill: text (raw status) + color dual encoding, hover explains the semantics. */
export function StatusPill({ status }: { status?: string }) {
  const { t } = useTranslation();
  if (!status?.trim()) return null;
  const tone = statusTone(status);
  return (
    <span
      className={cn("vigil-status-pill", statusPillClass(status))}
      title={t("common.statusTitle", { label: t(`common.tone.${tone}`) })}
    >
      <span className={cn("vigil-status-dot", statusDotClass(status))} />
      {status}
    </span>
  );
}

/** Bare status dot (compact spots like link summaries): color + hover explanation; use StatusPill when text is needed. */
export function StatusDot({ status }: { status?: string }) {
  const { t } = useTranslation();
  const tone = statusTone(status);
  return (
    <span
      className={cn("vigil-status-dot", statusDotClass(status))}
      title={t("common.statusTitle", { label: t(`common.tone.${tone}`) })}
    />
  );
}

/** Status text label (paired with StatusDot: dot + word, dual encoding). */
export function StatusText({ status }: { status?: string }) {
  const { t } = useTranslation();
  const tone = statusTone(status);
  return <span className="text-[var(--vigil-muted)]">{t(`common.tone.${tone}`)}</span>;
}
