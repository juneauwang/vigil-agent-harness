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

/**
 * D2 /schedules：cron job 的 PUT/POST 契约（页面只传 updates，包裹由 api 层做）。
 */
describe("cron jobs（D2 /schedules）", () => {
  it("updateCronJob → PUT /api/cron/jobs/{id}，body 是 {updates:{...}}", async () => {
    const job = { id: "job-1", name: "digest", script_args: ["--window", "1d"] };
    const fetchMock = stubFetch(200, job);
    const res = await api.updateCronJob("job-1", { script_args: ["--window", "1d"] });
    expect(res).toMatchObject({ id: "job-1" });

    const [url, init] = (fetchMock as unknown as ReturnType<typeof vi.fn>).mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/cron/jobs/job-1");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({
      updates: { script_args: ["--window", "1d"] },
    });
  });

  it("pause/resume/trigger → POST 到子路径；delete → DELETE", async () => {
    const fetchMock = stubFetch(200, { ok: true });
    await api.pauseCronJob("job-1");
    await api.resumeCronJob("job-1");
    await api.triggerCronJob("job-1");
    await api.deleteCronJob("job-1");

    const calls = (fetchMock as unknown as ReturnType<typeof vi.fn>).mock.calls as Array<[string, RequestInit]>;
    expect(calls.map(([url, init]) => `${init.method} ${url}`)).toEqual([
      "POST /api/cron/jobs/job-1/pause",
      "POST /api/cron/jobs/job-1/resume",
      "POST /api/cron/jobs/job-1/trigger",
      "DELETE /api/cron/jobs/job-1",
    ]);
  });

  it("instantiateBlueprint → POST body {blueprint, values}", async () => {
    const fetchMock = stubFetch(200, { id: "job-9", name: "Weekly work log (digest)" });
    await api.instantiateBlueprint("weekly-oplog", { window: "1d" });

    const [url, init] = (fetchMock as unknown as ReturnType<typeof vi.fn>).mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/cron/blueprints/instantiate");
    expect(JSON.parse(String(init.body))).toEqual({
      blueprint: "weekly-oplog",
      values: { window: "1d" },
    });
  });
});
