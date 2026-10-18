import type { ReactNode } from "react";
import { cn } from "@/lib/ops";

/**
 * Shared route hero: eyebrow + icon tile + title + description + meta/actions,
 * with an optional toolbar row underneath. Keeping this in one component is
 * what makes the nine routes feel like one console instead of nine pages.
 */
export function PageHeader({
  icon,
  eyebrow,
  title,
  description,
  meta,
  actions,
  children,
  size = "md",
  className,
}: {
  icon: ReactNode;
  eyebrow: string;
  title: string;
  description?: string;
  meta?: ReactNode;
  actions?: ReactNode;
  /** Optional toolbar row (filters / search / view switch / session picker). */
  children?: ReactNode;
  size?: "md" | "sm";
  className?: string;
}) {
  return (
    <section className={cn("page-header", size === "sm" && "page-header--sm", className)}>
      <div className="page-header-top">
        <div className="page-header-icon">{icon}</div>
        <div className="min-w-0 flex-1">
          <div className="page-eyebrow">{eyebrow}</div>
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="page-title">{title}</h1>
            {meta}
          </div>
          {description ? <p className="page-subtitle">{description}</p> : null}
        </div>
        {actions ? <div className="page-actions">{actions}</div> : null}
      </div>
      {children ? <div className="page-toolbar">{children}</div> : null}
    </section>
  );
}
