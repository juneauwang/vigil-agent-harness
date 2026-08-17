// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import ApprovalModal from "./ApprovalModal";
import { api } from "@/lib/api";
import type { ApprovalItem } from "@/lib/api";
import type { ApprovalSnapshot } from "@/lib/approvalPoller";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      approveApproval: vi.fn(),
      denyApproval: vi.fn(),
      getApprovals: vi.fn(),
    },
  };
});

const apiMock = api as unknown as {
  approveApproval: ReturnType<typeof vi.fn>;
  denyApproval: ReturnType<typeof vi.fn>;
  getApprovals: ReturnType<typeof vi.fn>;
};

const PENDING: ApprovalItem = {
  id: "apv_1",
  command: "rm -rf /var/tmp/vigil-demo",
  description: "delete in root path",
  env: "test",
  grade: "L2",
  status: "pending",
  created_at: "2026-08-17T00:00:00Z",
};

let snap: ApprovalSnapshot;
let container: HTMLDivElement;
let root: Root;

vi.mock("@/lib/approvalPoller", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/approvalPoller")>();
  return {
    ...actual,
    useApprovalSnapshot: () => snap,
    approvalPoller: {
      ...actual.approvalPoller,
      refresh: vi.fn(),
    },
  };
});

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  snap = { approvals: [PENDING], total: 1, added: [PENDING] };
  apiMock.approveApproval.mockResolvedValue({ status: "approved" });
  apiMock.denyApproval.mockResolvedValue({ status: "denied" });
});

afterEach(async () => {
  await act(async () => {
    root.unmount();
  });
  container.remove();
  vi.clearAllMocks();
});

async function render() {
  await act(async () => {
    root.render(<ApprovalModal />);
  });
}

describe("批四十一 §2 审批弹窗批准即关 + 防连点", () => {
  it("批准成功 → 弹窗关闭（dequeue 弹下一个/无下一个则消失）", async () => {
    await render();
    expect(container.textContent).toContain("该命令需要审批");
    const approveBtn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.includes("批准"))!;
    expect(approveBtn).toBeTruthy();
    await act(async () => {
      approveBtn.click();
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(apiMock.approveApproval).toHaveBeenCalledTimes(1);
    expect(apiMock.approveApproval).toHaveBeenCalledWith("apv_1", "once");
    // 队列已清空 → 弹窗消失
    expect(container.textContent).not.toContain("该命令需要审批");
  });

  it("连点只发一次请求（提交中按钮禁用）", async () => {
    await render();
    const approveBtn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.includes("批准"))!;
    let resolveApprove: (v: unknown) => void = () => {};
    apiMock.approveApproval.mockImplementation(() => new Promise((res) => { resolveApprove = res; }));
    await act(async () => {
      approveBtn.click();
      approveBtn.click(); // 第二次点击：busy 中按钮已禁用
    });
    expect(apiMock.approveApproval).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolveApprove({ status: "approved" });
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(container.textContent).not.toContain("该命令需要审批");
  });
});

describe("批四十一 §6 弹窗长命令可滚动/展开全文", () => {
  it("多行 python 命令默认折叠预览，展开显示完整缩进文本", async () => {
    const longCmd = "# 批量巡检\nfor h in hosts:\n    print(h)\n    run(h, dry_run=True)\n# 结束";
    snap = { approvals: [{ ...PENDING, command: longCmd }], total: 1, added: [{ ...PENDING, command: longCmd }] };
    await render();
    // 默认折叠：不输出完整多行（只显示首行截断）
    expect(container.textContent).not.toContain("for h in hosts");
    // 展开全文
    const expandBtn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.includes("展开全文"))!;
    expect(expandBtn).toBeTruthy();
    await act(async () => {
      expandBtn.click();
    });
    expect(container.textContent).toContain(longCmd);
    // 保留缩进（\n 不丢失）
    expect(container.textContent).toContain("    print(h)");
  });
});
