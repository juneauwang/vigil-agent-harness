// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { translateBackendMessage } from "./backendMsg";
import i18n, { switchLang } from "@/i18n";

async function setLang(lang: "zh" | "en") {
  switchLang(lang);
  await i18n.changeLanguage(lang);
}

describe("translateBackendMessage (Option C map)", () => {
  it("zh passes backend messages through untouched (backend emits zh)", async () => {
    await setLang("zh");
    const msg = "Prometheus 请求失败: connection refused";
    expect(translateBackendMessage(msg, "zh")).toBe(msg);
  });

  it("en maps known monitoring messages; dynamic tails are preserved", async () => {
    await setLang("en");
    expect(
      translateBackendMessage("Prometheus 请求失败: connection refused", "en"),
    ).toBe("Prometheus request failed: connection refused");
    expect(
      translateBackendMessage(
        "未配置 Prometheus（config ops.prometheus.endpoint），仅健康探测可用。",
        "en",
      ),
    ).toBe("Prometheus not configured (config ops.prometheus.endpoint); only health probing is available.");
    expect(
      translateBackendMessage(
        "未配置 Alertmanager（config ops.prometheus.alertmanager），仅健康探测可用。",
        "en",
      ),
    ).toBe("Alertmanager not configured (config ops.prometheus.alertmanager); only health probing is available.");
    expect(
      translateBackendMessage("Prometheus 查询失败 HTTP 502: bad gateway", "en"),
    ).toBe("Prometheus query failed HTTP 502: bad gateway");
  });

  it("en preserves an error-code head prefix ([code] message)", async () => {
    await setLang("en");
    expect(
      translateBackendMessage("[prometheus_unavailable] 未配置 Prometheus（config ops.prometheus.endpoint），仅健康探测可用。", "en"),
    ).toBe(
      "[prometheus_unavailable] Prometheus not configured (config ops.prometheus.endpoint); only health probing is available.",
    );
  });

  it("en translates matrix coverage warnings with interpolation", async () => {
    await setLang("en");
    expect(
      translateBackendMessage("matrix.prod 漏配 2 个动作（restart, scale）——默认 approve（保守）。", "en"),
    ).toBe("matrix.prod is missing 2 actions (restart, scale) — defaulting to approve (conservative).");
    expect(
      translateBackendMessage("sources.prod.restart: 来源 'legacy' 未知，按原样保留。", "en"),
    ).toBe("sources.prod.restart: unknown source 'legacy'; kept as-is.");
  });

  it("en maps approval registry messages", async () => {
    await setLang("en");
    expect(
      translateBackendMessage("审批已超时（wait 策略：不自动批准，命令保持 pending）", "en"),
    ).toBe("Approval timed out (wait policy: no auto-approval, command stays pending)");
    expect(translateBackendMessage("审批不存在: apv_1", "en")).toBe("Approval not found: apv_1");
  });

  it("unknown messages pass through unchanged in en mode", async () => {
    await setLang("en");
    const msg = "某个未映射的后端消息 detail=42";
    expect(translateBackendMessage(msg, "en")).toBe(msg);
    expect(translateBackendMessage("", "en")).toBe("");
  });
});
