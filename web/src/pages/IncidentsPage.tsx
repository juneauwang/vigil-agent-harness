import { useEffect, useState } from "react";
import { TriangleAlert } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { isMockEnabled } from "@/lib/mock";
import { EmptyState } from "@/components/EmptyState";

/**
 * Incidents 事件页（批二十八契约）：Vigil 无监控/告警体系，数据源不存在——
 * 本页只调 GET /api/incidents（后端返回空列表 + schema_version），
 * 诚实空态，不编造 incident 数据。
 */
export default function IncidentsPage() {
  const mock = isMockEnabled();
  const [schemaVersion, setSchemaVersion] = useState<number | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    if (mock) {
      setSchemaVersion(1);
      setTotal(0);
      return;
    }
    api
      .getIncidents({ limit: 50 })
      .then((resp) => {
        if (!alive) return;
        if (resp.error) {
          setError(resp.error.message ?? "加载失败");
          return;
        }
        setSchemaVersion(resp.schema_version ?? null);
        setTotal(resp.total ?? 0);
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setError(
          e instanceof ApiError
            ? `[${e.code}] ${e.message}（后端批二十八未就绪时显示此提示）`
            : e instanceof Error
              ? e.message
              : String(e),
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
        {schemaVersion !== null && (
          <span className="rounded-full bg-[var(--vigil-muted-bg)] px-2 py-0.5 text-xs text-[var(--vigil-muted)]">
            schema v{schemaVersion} · {total ?? 0} 条
          </span>
        )}
      </div>

      <EmptyState
        icon={<TriangleAlert className="size-6" />}
        title="暂无 Incident 数据"
        description={
          error ??
          "Vigil 无监控/告警体系（不接 Prometheus/Zabbix），Incidents 数据源不存在；本页按契约返回空列表 + schema。"
        }
        hint={
          "未来数据源候选（独立排期）：runbook 触发记录 / trajectory 异常事件聚合（exit_code≠0 命令 + 审批 deny 事件）；告警旁路对接为炎龙调研借鉴项。"
        }
        className="py-16"
      />
    </div>
  );
}
