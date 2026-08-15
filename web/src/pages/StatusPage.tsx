import { useEffect, useState, type ReactNode } from "react";
import {
  Activity,
  CalendarDays,
  Cpu,
  Database,
  Gauge,
  HeartPulse,
  Layers,
  RefreshCw,
  Server,
  Shield,
  ShieldAlert,
} from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { api } from "@/lib/api";
import type { StatusResponse } from "@/lib/api";
import { cn } from "@/lib/utils";

function StatCard({
  icon,
  label,
  value,
  sub,
}: {
  icon: ReactNode;
  label: string;
  value: ReactNode;
  sub?: ReactNode;
}) {
  return (
    <Card className="shadow-sm">
      <CardContent className="p-4">
        <div className="mb-1 flex items-center gap-2 text-xs text-muted-foreground">
          {icon}
          {label}
        </div>
        <div className="text-lg font-semibold">{value}</div>
        {sub && <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>}
      </CardContent>
    </Card>
  );
}

function Pill({ ok, children }: { ok: boolean; children: ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs",
        ok ? "bg-emerald-500/15 text-emerald-400" : "bg-red-500/15 text-red-400",
      )}
    >
      <span className={cn("size-1.5 rounded-full", ok ? "bg-emerald-400" : "bg-red-400")} />
      {children}
    </span>
  );
}

export default function StatusPage() {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = () => {
    setLoading(true);
    setError(null);
    api
      .getStatus()
      .then((st) => setStatus(st))
      .catch((e: unknown) => {
        setStatus(null);
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  if (loading) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center gap-2 text-sm text-muted-foreground">
        <Spinner />
        <span>加载状态…</span>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-7xl p-4 lg:p-6">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <Gauge className="size-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">状态</h1>
        </div>
        <Button ghost size="sm" onClick={load} aria-label="刷新">
          <RefreshCw className="size-4" />
        </Button>
        <span className="ml-auto text-xs text-muted-foreground">只读</span>
      </div>

      {error ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
            <ShieldAlert className="size-8 text-muted-foreground" />
            <div className="text-sm text-muted-foreground">{error}</div>
            <Button size="sm" onClick={load}>
              重试
            </Button>
          </CardContent>
        </Card>
      ) : status ? (
        <div className="space-y-6">
          {/* Health row */}
          <div className="flex flex-wrap gap-2">
            <Pill ok={Boolean(status.gateway_running)}>
              gateway {status.gateway_state ?? (status.gateway_running ? "running" : "stopped")}
            </Pill>
            <Pill ok={!status.auth_required}>loopback 模式</Pill>
            <Pill ok={status.config_version >= (status.latest_config_version ?? status.config_version)}>
              配置 v{status.config_version}
              {status.latest_config_version !== undefined &&
                status.latest_config_version > status.config_version &&
                "（有更新）"}
            </Pill>
          </div>

          {/* Core stats */}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <StatCard
              icon={<Server className="size-3.5" />}
              label="版本"
              value={status.version}
              sub={<span className="inline-flex items-center gap-1"><CalendarDays className="size-3" />{status.release_date}</span>}
            />
            <StatCard
              icon={<Activity className="size-3.5" />}
              label="活跃会话"
              value={status.active_sessions ?? 0}
            />
            <StatCard
              icon={<Database className="size-3.5" />}
              label="已连接平台"
              value={Object.keys(status.gateway_platforms ?? {}).length}
              sub={Object.keys(status.gateway_platforms ?? {}).join("、") || "-"}
            />
            <StatCard
              icon={<Layers className="size-3.5" />}
              label="配置"
              value={`v${status.config_version}`}
              sub={
                status.latest_config_version !== undefined &&
                status.latest_config_version > status.config_version
                  ? `最新 v${status.latest_config_version}`
                  : "已是最新"
              }
            />
          </div>

          {/* Detail grid */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Card className="shadow-sm">
              <CardContent className="p-4">
                <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold">
                  <Cpu className="size-4 text-muted-foreground" /> 运行环境
                </h2>
                <dl className="space-y-1.5 text-sm">
                  <Row k="配置版本" v={`v${status.config_version}`} />
                  <Row k="Gateway 状态" v={`${status.gateway_state ?? "-"}${status.gateway_running ? "（运行中）" : ""}`} />
                  {status.gateway_pid != null && <Row k="Gateway PID" v={String(status.gateway_pid)} />}
                  <Row k="认证模式" v={status.auth_required ? "OAuth 门控" : "loopback"} />
                  <Row k="活跃会话" v={String(status.active_sessions ?? 0)} />
                </dl>
              </CardContent>
            </Card>

            <Card className="shadow-sm">
              <CardContent className="p-4">
                <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold">
                  <Shield className="size-4 text-muted-foreground" /> 组件健康
                </h2>
                <dl className="space-y-1.5 text-sm">
                  <Row k="gateway" v={status.gateway_running ? "ok" : "degraded"} />
                  <Row k="dashboard" v="ok" />
                  <Row k="认证门" v={status.auth_required ? "on（OAuth）" : "off（loopback）"} />
                </dl>
              </CardContent>
            </Card>
          </div>

          <p className="text-xs text-muted-foreground">
            <HeartPulse className="mr-1 inline size-3" />
            数据来自 <code className="font-mono">/api/status</code>（公开只读探针）。
          </p>
        </div>
      ) : null}
    </div>
  );
}

function Row({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="flex gap-2 border-b border-dotted border-border/60 py-1 last:border-b-0">
      <span className="min-w-32 shrink-0 text-muted-foreground">{k}</span>
      <span className="text-foreground/85">{v}</span>
    </div>
  );
}
