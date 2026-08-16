import { describe, expect, it } from "vitest";

import {
  STATUS_FILTERS,
  aggregateTopologyStats,
  formatUptime,
  lastSeenInfo,
  matchesSearch,
  statusMatchesFilter,
  statusTone,
  yamlPreview,
} from "./ops";

describe("statusTone", () => {
  it("maps healthy states to ok", () => {
    for (const s of ["running", "up", "healthy", "ok", "RUNNING"]) {
      expect(statusTone(s)).toBe("ok");
    }
  });
  it("maps warning-ish states to warn", () => {
    for (const s of ["warning", "degraded", "unhealthy"]) {
      expect(statusTone(s)).toBe("warn");
    }
  });
  it("maps failure states to error", () => {
    for (const s of ["error", "failed", "critical", "down", "crash"]) {
      expect(statusTone(s)).toBe("error");
    }
  });
  it("maps missing / stopped / unknown to offline", () => {
    expect(statusTone(undefined)).toBe("offline");
    expect(statusTone("")).toBe("offline");
    expect(statusTone("stopped")).toBe("offline");
    expect(statusTone("unknown")).toBe("offline");
  });
});

describe("statusMatchesFilter", () => {
  it("all matches everything", () => {
    expect(statusMatchesFilter("running", "all")).toBe(true);
    expect(statusMatchesFilter(undefined, "all")).toBe(true);
  });
  it("filters by tone", () => {
    expect(statusMatchesFilter("running", "ok")).toBe(true);
    expect(statusMatchesFilter("degraded", "warn")).toBe(true);
    expect(statusMatchesFilter("failed", "error")).toBe(true);
    expect(statusMatchesFilter("stopped", "offline")).toBe(true);
    expect(statusMatchesFilter("running", "error")).toBe(false);
  });
});

describe("STATUS_FILTERS", () => {
  it("exposes the five filters", () => {
    expect(STATUS_FILTERS.map((f) => f.id)).toEqual([
      "all", "ok", "warn", "error", "offline",
    ]);
  });
});

describe("matchesSearch", () => {
  it("matches name / type / env case-insensitively", () => {
    const card = { name: "node1", type: "k8s", env: "prod" };
    expect(matchesSearch(card, "node")).toBe(true);
    expect(matchesSearch(card, "K8S")).toBe(true);
    expect(matchesSearch(card, "prod")).toBe(true);
    expect(matchesSearch(card, "nope")).toBe(false);
    expect(matchesSearch(card, "")).toBe(true);
  });
});

describe("aggregateTopologyStats", () => {
  it("counts clusters / hosts / services", () => {
    const stats = aggregateTopologyStats({
      clusters: [{ name: "a" }, { name: "b" }],
      hosts: [{ services: [1, 2] }, { services: [3] }, { services: [] }],
    });
    expect(stats).toEqual({ clusters: 2, hosts: 3, services: 3 });
  });
});

describe("yamlPreview", () => {
  it("renders scalars and lists", () => {
    const out = yamlPreview({
      name: "harbor-restart",
      env: "prod",
      steps: [{ id: "diagnose", commands: ["docker ps"] }],
    });
    expect(out).toContain("name: harbor-restart");
    expect(out).toContain("env: prod");
    expect(out).toContain("steps:");
    expect(out).toContain("- id: diagnose");
    expect(out).toContain("commands:");
    expect(out).toContain("- docker ps");
  });
  it("quotes strings needing escaping and keeps redacted markers intact", () => {
    const out = yamlPreview({ title: "a: b", note: "[已过滤]" });
    expect(out).toContain('title: "a: b"');
    expect(out).toContain("note: [已过滤]");
  });
});

describe("formatUptime", () => {
  it("formats seconds into compact durations", () => {
    expect(formatUptime(0)).toBe("0s");
    expect(formatUptime(45)).toBe("45s");
    expect(formatUptime(3600)).toBe("1h");
    expect(formatUptime(43200)).toBe("12h");
    expect(formatUptime(90000)).toBe("1d 1h");
  });
  it("handles missing / invalid values", () => {
    expect(formatUptime(undefined)).toBe("-");
    expect(formatUptime(null)).toBe("-");
    expect(formatUptime(-1)).toBe("-");
  });
});

describe("lastSeenInfo（批三十五活性）", () => {
  const NOW = 1_800_000_000_000;
  it("无记录 → 未探测", () => {
    expect(lastSeenInfo(undefined, NOW).label).toBe("未探测");
    expect(lastSeenInfo(0, NOW).label).toBe("未探测");
    expect(lastSeenInfo(undefined, NOW).tone).toBe("offline");
  });
  it("≤ 阈值 → 在线 · X 分钟前活跃", () => {
    expect(lastSeenInfo((NOW - 0.5 * 60_000) / 1000, NOW).label).toBe("在线 · 刚刚活跃");
    expect(lastSeenInfo((NOW - 5 * 60_000) / 1000, NOW).label).toBe("在线 · 5 分钟前活跃");
    expect(lastSeenInfo((NOW - 10 * 60_000) / 1000, NOW).label).toBe("在线 · 10 分钟前活跃");
    expect(lastSeenInfo((NOW - 5 * 60_000) / 1000, NOW).tone).toBe("ok");
  });
  it("超过阈值 → 离线 · 已 X 分钟无活动", () => {
    expect(lastSeenInfo((NOW - 11 * 60_000) / 1000, NOW).label).toBe("离线 · 已 11 分钟无活动");
    expect(lastSeenInfo((NOW - 11 * 60_000) / 1000, NOW).tone).toBe("offline");
  });
});
