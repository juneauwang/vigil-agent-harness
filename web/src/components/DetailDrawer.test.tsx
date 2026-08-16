import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import DetailDrawer from "./DetailDrawer";
import type { GraphEntityRef } from "@/lib/topologyGraph";

const ENTITY: GraphEntityRef = {
  kind: "service",
  name: "order-db",
  card: { name: "order-db", env: "prod", status: "running", kind: "service" },
  detail: {
    type: "db",
    endpoint: "10.0.0.5:5432",
    note: "业务请求链路：入口 → 网关 → 服务 → 存储",
  },
};

describe("详情抽屉（批三十五）", () => {
  it("null → 不渲染", () => {
    expect(renderToStaticMarkup(<DetailDrawer entity={null} onClose={() => {}} />)).toBe("");
  });

  it("渲染标题（实体名 + env 徽章 + 状态 pill）+ DetailTree 内容 + 关闭按钮", () => {
    const html = renderToStaticMarkup(<DetailDrawer entity={ENTITY} onClose={() => {}} />);
    expect(html).toContain("服务");
    expect(html).toContain("order-db");
    expect(html).toContain("prod"); // EnvBadge
    expect(html).toContain("running"); // StatusPill
    expect(html).toContain("10.0.0.5:5432"); // DetailTree 值
    expect(html).toContain("业务请求链路"); // 长文本不折行渲染
    expect(html).toContain('aria-label="关闭详情"');
  });

  it("遮罩点击关闭（aria-label 供 E2E 定位）", () => {
    const html = renderToStaticMarkup(<DetailDrawer entity={ENTITY} onClose={() => {}} />);
    expect(html).toContain('aria-label="关闭详情遮罩"');
  });
});
