// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import ApprovalsPage from "./ApprovalsPage";
import { api } from "@/lib/api";
import type { ApprovalItem } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getApprovals: vi.fn(),
      approveApproval: vi.fn(),
      denyApproval: vi.fn(),
    },
  };
});

const LONG_CMD = [
  "#!/usr/bin/env python3",
  "import requests",
  "for host in hosts:",
  "    resp = requests.get(f'https://{host}/health')",
  "    print(host, resp.status_code)",
  "# 长命令的完整结尾",
].join("\n");

const ITEM: ApprovalItem = {
  id: "apv_long",
  command: LONG_CMD,
  description: "批量巡检所有主机健康状态（含完整说明不截断）",
  env: "test",
  grade: "L2",
  status: "pending",
  created_at: "2026-08-17T00:00:00Z",
};

const apiMock = api as unknown as {
  getApprovals: ReturnType<typeof vi.fn>;
};

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  apiMock.getApprovals.mockResolvedValue({ approvals: [ITEM], total: 1, has_more: false });
});

afterEach(async () => {
  await act(async () => {
    root.unmount();
  });
  container.remove();
  vi.clearAllMocks();
});

describe("批四十一 §6 审批记录完整详情", () => {
  it("长命令默认折叠，展开后 DOM 内完整多行文本不截断（保留缩进）", async () => {
    await act(async () => {
      root.render(<ApprovalsPage />);
    });
    await act(async () => {
      await Promise.resolve();
    });
    // 默认折叠：不出现中间行
    expect(container.textContent).not.toContain("for host in hosts:");
    const expandBtn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.includes("展开全文"))!;
    expect(expandBtn).toBeTruthy();
    await act(async () => {
      expandBtn.click();
    });
    await act(async () => {
      await Promise.resolve();
    });
    const text = container.textContent ?? "";
    expect(text).toContain(LONG_CMD);
    expect(text).toContain("    resp = requests.get");
    expect(text).toContain("批量巡检所有主机健康状态");
  });

  it("短命令无需展开直接完整显示", async () => {
    apiMock.getApprovals.mockResolvedValue({
      approvals: [{ ...ITEM, id: "apv_short", command: "kubectl get pods" }],
      total: 1,
      has_more: false,
    });
    await act(async () => {
      root.render(<ApprovalsPage />);
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(container.textContent).toContain("kubectl get pods");
    expect(container.textContent).not.toContain("展开全文");
  });
});
