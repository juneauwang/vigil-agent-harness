import { describe, expect, it } from "vitest";
import i18n, { LANG_STORAGE_KEY, currentLang, switchLang } from "./index";
import zh from "./zh";
import en from "./en";

// node env: minimal localStorage shim so persistence logic is exercised.
const store = new Map<string, string>();
(globalThis as { localStorage?: Storage }).localStorage ??= {
  getItem: (k: string) => store.get(k) ?? null,
  setItem: (k: string, v: string) => void store.set(k, v),
  removeItem: (k: string) => void store.delete(k),
  clear: () => store.clear(),
  key: () => null,
  get length() { return store.size; },
} as Storage;

function keyPaths(obj: Record<string, unknown>, prefix = ""): string[] {
  return Object.entries(obj).flatMap(([k, v]) =>
    v && typeof v === "object" ? keyPaths(v as Record<string, unknown>, `${prefix}${k}.`) : [`${prefix}${k}`],
  );
}

describe("i18n resources", () => {
  it("en.ts has exactly the same key set as zh.ts (no missing translations)", () => {
    expect(keyPaths(en).sort()).toEqual(keyPaths(zh).sort());
  });

  it("spot-check: exact original UI copy preserved verbatim in zh.ts", () => {
    const z = zh as unknown as Record<string, Record<string, string>>;
    expect(z.chat.placeholder).toBe("和 Vigil 说点什么，如「看下拓扑有几台主机」");
    expect(z.chat.busyStill).toBe("服务端仍显示忙碌（可能仍在收尾）。可在会话列表刷新或稍后重试。");
    expect(z.matrix.headerNote).toBe("矩阵是人工安全资产，修改即审计（LLM 只有 matrix_query 只读）");
    expect(z.runbooks.noHistory).toBe("暂无执行记录（交互 / 定时执行都会落到 runtime/runbook_executions.jsonl）");
    expect(z.monitoring.noAlertsHint).toBe("watch 采集的告警流见 Incidents 页——两者语义不同：Incidents = 采集归档，本页 = 实时状态。");
    expect(z.topology.resetConfirm).toBe("将清空全部拓扑数据（{{hosts}} 主机 / {{clusters}} 集群 + 服务/实体目录），回到未初始化状态。此操作不可撤销，确认？");

    // Round 2: audit / incidents / overview / lib spot-checks (3 per page).
    expect(z.audit.sessionsHeading).toBe("会话（{{n}}）");
    expect(z.audit.readonlyNote).toBe("只读 · 内容已脱敏");
    expect(z.audit.eventDetailHeading).toBe("事件 #{{seq}} · {{type}} · {{ts}}");
    expect(z.incidents.noAlertsDesc).toBe("watch 采集层（ops.watch）接入 Alertmanager 后，活跃告警会出现在这里。");
    expect(z.incidents.processedBadge).toBe("已处理");
    expect(z.incidents.fldCollectedAt).toBe("collected_at：");
    expect(z.overview.riskSub).toBe("高危 {{total}} 已覆盖 {{covered}}（覆盖率 {{pct}}%）");
    expect(z.overview.openTopology).toBe("打开资产拓扑");
    expect(z.overview.noTopologyHint).toBe("先运行 vigil topo-discover 发现主机");
    expect(z.lib.notifyTitle).toBe("Vigil 需要审批");
    expect(z.lib.chatErrorFallback).toBe("对话出错");
  });
});

describe("i18n language switching", () => {
  it("defaults to zh; switchLang('en') swaps strings and persists to localStorage", async () => {
    expect(currentLang()).toBe("zh");
    expect(i18n.t("common.loading")).toBe("加载中…");
    expect(i18n.t("chat.send")).toBe("发送");

    switchLang("en");
    await i18n.changeLanguage("en"); // ensure the async swap settles
    expect(currentLang()).toBe("en");
    expect(i18n.t("common.loading")).toBe("Loading…");
    expect(i18n.t("chat.send")).toBe("Send");
    expect(store.get(LANG_STORAGE_KEY)).toBe("en");

    switchLang("zh");
    await i18n.changeLanguage("zh");
    expect(i18n.t("chat.send")).toBe("发送");
    expect(store.get(LANG_STORAGE_KEY)).toBe("zh");
  });
});
