import { describe, expect, it } from "vitest";

import {
  MOCK_APPROVALS,
  MOCK_AUDIT_EVENTS,
  MOCK_AUDIT_SESSIONS,
} from "./mock";

describe("mock 数据（契约红线：文档 IP + 占位符，零真实凭据）", () => {
  it("all mock payloads contain no credential material", () => {
    const blob = JSON.stringify([
      MOCK_APPROVALS,
      MOCK_AUDIT_EVENTS,
      MOCK_AUDIT_SESSIONS,
    ]);
    for (const leak of [".pem", "password", "passphrase", "~/.ssh", "id_rsa", "secret", "token=", "Harbor12345"]) {
      expect(blob).not.toContain(leak);
    }
  });
  it("uses documentation IPs only (203.0.113.x)", () => {
    const blob = JSON.stringify([MOCK_APPROVALS, MOCK_AUDIT_EVENTS]);
    const docIps = blob.match(/203\.0\.113\.\d+/g) ?? [];
    expect(docIps.length).toBeGreaterThan(0);
    // 不允许真实内网/公网 IP 样值
    expect(blob).not.toMatch(/10\.0\.\d+\.\d+|192\.168\.\d+\.\d+/);
  });
  it("audit event output_preview stays within preview limit", () => {
    for (const ev of MOCK_AUDIT_EVENTS) {
      const preview = ev.result?.output_preview ?? "";
      expect(preview.length).toBeLessThanOrEqual(500);
    }
  });
});
