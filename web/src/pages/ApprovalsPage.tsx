import { useCallback, useEffect, useState } from "react";
import { Check, Clock, ShieldCheck, X, XCircle } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import type { ApprovalItem, ApprovalScope } from "@/lib/api";
import { isMockEnabled, MOCK_APPROVALS } from "@/lib/mock";
import { cn } from "@/lib/ops";

/**
 * Approvals 审批中心（批二十八契约）：
 * GET /api/approvals（env/status 过滤 + limit/offset 分页，total/has_more）
 * + POST /api/approvals/{id}/approve（scope：once 默认；session/permanent
 * 仅 allow_* 时可选）+ deny；超时 → 409 timeout（fail-closed）。
 * mock 模式（localStorage vigil-mock=1）用文档 IP 占位数据。
 */
export default function ApprovalsPage() {
  const mock = isMockEnabled();
  const [items, setItems] = useState<ApprovalItem[]>([]);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [envFilter, setEnvFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<"pending" | "resolved" | "">("");
  const [offset, setOffset] = useState(0);
  const [limit] = useState(50);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [scopeSel, setScopeSel] = useState<Record<string, ApprovalScope>>({});
  const [mockNote] = useState(mock);

  const load = useCallback(
    (nextOffset = 0) => {
      setError(null);
      const p: { env?: string; status?: string; limit?: number; offset?: number } = {
        limit,
        offset: nextOffset,
      };
      if (envFilter) p.env = envFilter;
      if (statusFilter) p.status = statusFilter;
      if (mock) {
        let rows = [...MOCK_APPROVALS];
        if (envFilter) rows = rows.filter((a) => a.env === envFilter);
        if (statusFilter) {
          rows = rows.filter((a) =>
            statusFilter === "pending" ? a.status === "pending" : a.status !== "pending",
          );
        }
        const page = rows.slice(nextOffset, nextOffset + limit);
        setItems(page);
        setTotal(rows.length);
        setHasMore(nextOffset + page.length < rows.length);
        setOffset(nextOffset);
        return;
      }
      api
        .getApprovals(p)
        .then((resp) => {
          if (resp.error) {
            setError(resp.error.message ?? "加载失败");
            return;
          }
          setItems(resp.approvals ?? []);
          setTotal(resp.total ?? 0);
          setHasMore(Boolean(resp.has_more));
          setOffset(nextOffset);
        })
        .catch((e: unknown) => {
          setError(
            e instanceof ApiError
              ? `[${e.code}] ${e.message}`
              : e instanceof Error
                ? e.message
                : String(e),
          );
        });
    },
    [envFilter, statusFilter, limit, mock],
  );

  useEffect(() => {
    load(0);
  }, [load]);

  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusyId(id);
    setError(null);
    try {
      await fn();
      await load(offset);
    } catch (e) {
      if (e instanceof ApiError && e.code === "timeout") {
        setError(`审批 ${id} 已超时，不自动通过；命令保持待审批状态。`);
      } else if (e instanceof ApiError) {
        setError(`[${e.code}] ${e.message}`);
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setBusyId(null);
    }
  };

  const approve = (item: ApprovalItem) => {
    const scope = scopeSel[item.id] ?? "once";
    void act(item.id, () => (mock ? Promise.resolve() : api.approveApproval(item.id, scope)));
  };
  const deny = (item: ApprovalItem) => {
    void act(item.id, () => (mock ? Promise.resolve() : api.denyApproval(item.id)));
  };

  const statusBadge = (s: ApprovalItem["status"]) => {
    const map: Record<string, string> = {
      pending: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
      approved: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
      denied: "bg-red-500/15 text-red-600 dark:text-red-400",
      timeout: "bg-slate-500/15 text-slate-500",
    };
    return <span className={cn("rounded-full px-2 py-px text-[11px]", map[s] ?? map.timeout)}>{s}</span>;
  };

  const isPastTimeout = (item: ApprovalItem): boolean =>
    Boolean(item.status === "pending" && item.timeout_at && new Date(item.timeout_at).getTime() < Date.now());

  return (
    <div className="mx-auto w-full max-w-6xl">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <ShieldCheck className="size-5 text-[var(--vigil-muted)]" />
          <h1 className="text-lg font-semibold">Approvals</h1>
        </div>
        <span className="rounded-full bg-[var(--vigil-muted-bg)] px-2 py-0.5 text-xs text-[var(--vigil-muted)]">
          {total} 条
        </span>
        <span className="ml-auto text-xs text-[var(--vigil-muted)]">
          超时审批不自动通过
        </span>
      </div>

      {mockNote && (
        <div className="mb-3 inline-flex items-center gap-1.5 rounded border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[11px] text-amber-600 dark:text-amber-400">
          <Clock className="size-3" /> 模拟数据
        </div>
      )}

      {/* 过滤 + 分页工具条 */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <select
          value={envFilter}
          onChange={(e) => setEnvFilter(e.target.value)}
          className="vigil-input h-8 w-28 text-xs"
        >
          <option value="">env 全部</option>
          <option value="prod">prod</option>
          <option value="test">test</option>
          <option value="dev">dev</option>
          <option value="local">local</option>
        </select>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as typeof statusFilter)}
          className="vigil-input h-8 w-32 text-xs"
        >
          <option value="">状态全部</option>
          <option value="pending">pending</option>
          <option value="resolved">resolved</option>
        </select>
        <button type="button" onClick={() => load(0)} className="vigil-btn h-8 border border-[var(--vigil-border)]">
          刷新
        </button>
        <div className="ml-auto flex items-center gap-1 text-xs text-[var(--vigil-muted)]">
          <button
            type="button"
            disabled={offset === 0}
            onClick={() => load(Math.max(0, offset - limit))}
            className="vigil-btn h-6 px-2 disabled:opacity-40"
          >
            上一页
          </button>
          <span>
            {offset + 1}–{Math.min(offset + items.length, total)} / {total}
          </span>
          <button
            type="button"
            disabled={!hasMore}
            onClick={() => load(offset + limit)}
            className="vigil-btn h-6 px-2 disabled:opacity-40"
          >
            下一页
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-3 flex items-center gap-2 rounded-md border border-red-500/50 bg-red-500/10 px-3 py-2 text-xs">
          <XCircle className="size-4 shrink-0 text-red-500" />
          <span>{error}</span>
        </div>
      )}

      {items.length === 0 ? (
        <div className="rounded-md border border-dashed border-[var(--vigil-border)] p-12 text-center text-sm text-[var(--vigil-muted)]">
          暂无审批条目
          {error && <div className="mt-1 text-xs opacity-70"></div>}
        </div>
      ) : (
        <div className="vigil-card">
          <table className="vigil-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>命令 / 描述</th>
                <th>env</th>
                <th>grade</th>
                <th>状态</th>
                <th>创建</th>
                <th className="text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td className="font-mono text-[11px] text-[var(--vigil-muted)]">{item.id}</td>
                  <td>
                    <div className="max-w-[420px] truncate font-mono text-xs" title={item.command}>
                      {item.command}
                    </div>
                    {item.description && (
                      <div className="truncate text-[11px] text-[var(--vigil-muted)]">{item.description}</div>
                    )}
                  </td>
                  <td><span className={cn("vigil-env", item.env === "prod" ? "env-prod" : "env-test")}>{item.env ?? "-"}</span></td>
                  <td className="text-[var(--vigil-muted)]">{item.grade ?? "-"}</td>
                  <td>{statusBadge(item.status)}</td>
                  <td className="text-[11px] text-[var(--vigil-muted)]">
                    {item.created_at ? String(item.created_at).slice(0, 16) : "-"}
                    {isPastTimeout(item) && <div className="text-red-500">已超时</div>}
                  </td>
                  <td className="text-right">
                    {item.status === "pending" ? (
                      <div className="flex items-center justify-end gap-1.5">
                        {item.allow_session || item.allow_permanent ? (
                          <select
                            value={scopeSel[item.id] ?? "once"}
                            onChange={(e) => setScopeSel((m) => ({ ...m, [item.id]: e.target.value as ApprovalScope }))}
                            className="vigil-input h-6 w-20 text-[10px]"
                          >
                            <option value="once">once</option>
                            {item.allow_session && <option value="session">session</option>}
                            {item.allow_permanent && <option value="permanent">permanent</option>}
                          </select>
                        ) : null}
                        <button
                          type="button"
                          disabled={busyId === item.id || isPastTimeout(item)}
                          onClick={() => approve(item)}
                          className="vigil-btn vigil-btn-primary h-6 px-2 text-xs disabled:opacity-40"
                        >
                          <Check className="size-3" /> 批准
                        </button>
                        <button
                          type="button"
                          disabled={busyId === item.id || isPastTimeout(item)}
                          onClick={() => deny(item)}
                          className="vigil-btn h-6 border border-[var(--vigil-border)] px-2 text-xs disabled:opacity-40"
                        >
                          <X className="size-3" /> 拒绝
                        </button>
                      </div>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-xs text-[var(--vigil-muted)]">
                        {item.scope ? `scope: ${item.scope}` : "—"}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {items.length === 0 && !error && null}
    </div>
  );
}
