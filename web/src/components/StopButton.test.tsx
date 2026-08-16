import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import StopButton from "./StopButton";

describe("停止按钮（批三十六）", () => {
  it("busy 时渲染（aria-label 供 E2E 定位）", () => {
    const html = renderToStaticMarkup(<StopButton stopping={false} onStop={() => {}} />);
    expect(html).toContain('aria-label="停止"');
    expect(html).toContain("停止");
    expect(html).not.toContain("停止中");
  });

  it("stopping 时禁用并显示停止中", () => {
    const html = renderToStaticMarkup(<StopButton stopping onStop={() => {}} />);
    expect(html).toContain("disabled");
    expect(html).toContain("停止中");
  });

  it("红色警示样式（与发送并列的红系）", () => {
    const html = renderToStaticMarkup(<StopButton stopping={false} onStop={() => {}} />);
    expect(html).toContain("border-red-500/50");
  });
});
