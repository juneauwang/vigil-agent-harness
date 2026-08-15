import { useEffect, useState } from "react";
import { TriangleAlert } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { isMockEnabled } from "@/lib/mock";
import { EmptyState } from "@/components/EmptyState";

/**
 * Incidents 事件页：Vigil 暂无监控/告警接入，本页调用 /api/incidents
 * （后端就绪后返回事件列表；当前为空）。诚实空态，不编造事件数据。
 */
export default function IncidentsPage() {
  const mock = isMockEnabled();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    if (mock) return;
    api
      .getIncidents({ limit: 50 })
      .then((resp) => {
        if (!alive) return;
        if (resp.error) setError(resp.error.message ?? "");
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setError(
          e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e),
        );
      });
    return () => {
      alive = false;
    };
  }, [mock]);

  return (
    <div className="mx-auto w-full max-w-5xl">
      <div className="mb-4 flex items-center gap-2">
        <TriangleAlert className="size-5 text-[var(--vigil-muted)]" />
        <h1 className="text-lg font-semibold">Incidents</h1>
        <span className="text-xs text-[var(--vigil-muted)]">· 故障与告警</span>
      </div>

      <EmptyState
        icon={<TriangleAlert className="size-6" />}
        title="暂无事件"
        description={
          error || "告警接入后，这里会显示故障与异常记录。"
        }
        hint="需要监控系统（如 Prometheus）接入后才能产生事件；Vigil 暂未集成。"
        className="py-16"
      />
    </div>
  );
}
