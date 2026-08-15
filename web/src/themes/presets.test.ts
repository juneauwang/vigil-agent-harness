import { describe, expect, it } from "vitest";

import { BUILTIN_THEMES, defaultTheme, vigilConsoleDarkTheme, vigilConsoleTheme } from "./presets";

describe("Vigil Console theme (豆包 HTML 布局基准，第三批)", () => {
  it("is registered as a built-in theme", () => {
    expect(BUILTIN_THEMES["vigil-console"]).toBe(vigilConsoleTheme);
    expect(BUILTIN_THEMES["vigil-console-dark"]).toBe(vigilConsoleDarkTheme);
  });

  it("light variant: 底 #f6f8fa + 主色靛蓝 #2563eb + 状态色", () => {
    expect(vigilConsoleTheme.palette.background.hex).toBe("#f6f8fa");
    expect(vigilConsoleTheme.palette.midground.hex).toBe("#2563eb");
    expect(vigilConsoleTheme.colorOverrides?.success).toBe("#22c55e");
    expect(vigilConsoleTheme.colorOverrides?.warning).toBe("#f59e0b");
    expect(vigilConsoleTheme.colorOverrides?.destructive).toBe("#ef4444");
    expect(vigilConsoleTheme.colorOverrides?.border).toBe("#e1e5eb");
    expect(vigilConsoleTheme.layout.radius).toBe("0.375rem"); // 6px
  });

  it("uses Inter + JetBrains Mono typography", () => {
    expect(vigilConsoleTheme.typography.fontSans).toContain("Inter");
    expect(vigilConsoleTheme.typography.fontMono).toContain("JetBrains Mono");
    expect(vigilConsoleTheme.typography.fontUrl).toBeTruthy();
  });

  it("dark variant: #0f1c2d 系 + 主色亮化", () => {
    expect(vigilConsoleDarkTheme.name).toBe("vigil-console-dark");
    expect(vigilConsoleDarkTheme.palette.background.hex).toBe("#0f1c2d");
    expect(vigilConsoleDarkTheme.palette.midground.hex).toBe("#3b82f6");
    expect(vigilConsoleDarkTheme.terminalBackground).toBe("#0a1524");
  });

  it("terminal 深色底 #0f1c2d 系（light 变体）", () => {
    expect(vigilConsoleTheme.terminalBackground).toBe("#0f1c2d");
  });

  it("legacy default kept as optional theme (Vigil Blue-Grey Legacy)", () => {
    expect(defaultTheme.name).toBe("default");
    expect(defaultTheme.label).toContain("Legacy");
  });
});
