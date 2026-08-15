import { describe, expect, it } from "vitest";

import {
  STATUS_FILTERS,
  aggregateTopologyStats,
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
  it("exposes the five-direction filters", () => {
    expect(STATUS_FILTERS.map((f) => f.id)).toEqual([
      "all", "ok", "warn", "error", "offline",
    ]);
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
