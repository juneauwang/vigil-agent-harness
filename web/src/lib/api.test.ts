import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";

/**
 * 批三十六：interruptChatSession（停止按钮的后端调用）。
 * node 环境无 window → 无会话 token 头；stub fetch 断言方法/路径/错误信封。
 */

const SID = "chat_abc123";

function stubFetch(status: number, body: unknown) {
  const mock = vi.fn(async () => {
    const text = typeof body === "string" ? body : JSON.stringify(body);
    return {
      ok: status >= 200 && status < 300,
      status,
      text: async () => text,
      json: async () => (typeof body === "string" ? {} : body),
    } as unknown as Response;
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("interruptChatSession（批三十六）", () => {
  it("POST 到 /api/chat/sessions/{id}/interrupt，成功返回 body", async () => {
    const fetchMock = stubFetch(200, { status: "interrupted", chat_session_id: SID, approvals_cancelled: 0 });
    const res = await api.interruptChatSession(SID);
    expect(res).toMatchObject({ status: "interrupted", chat_session_id: SID });
    const [url, init] = (fetchMock as unknown as ReturnType<typeof vi.fn>).mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/chat/sessions/${SID}/interrupt`);
    expect(init.method).toBe("POST");
  });

  it("409 not_busy → 抛 ApiError（幂等语义，前端提示但不卡死）", async () => {
    stubFetch(409, { error: { code: "not_busy", message: "会话当前没有进行中的操作" } });
    await expect(api.interruptChatSession(SID)).rejects.toMatchObject({
      name: "ApiError",
      code: "not_busy",
      status: 409,
    } satisfies Partial<ApiError>);
  });

  it("404 未知会话 → 抛 ApiError", async () => {
    stubFetch(404, { error: { code: "not_found", message: "会话不存在: x" } });
    await expect(api.interruptChatSession("chat_nope")).rejects.toMatchObject({
      name: "ApiError",
      code: "not_found",
    });
  });
});
