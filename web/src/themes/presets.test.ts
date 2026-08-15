import { describe, expect, it } from "vitest";

import { BUILTIN_THEMES, defaultTheme, vigilConsoleDarkTheme, vigilConsoleTheme } from "./presets";

describe("Vigil Console theme (方向 1)", () => {
  it("is registered as a built-in theme", () => {
    expect(BUILTIN_THEMES["vigil-console"]).toBe(vigilConsoleTheme);
    expect(BUILTIN_THEMES["vigil-console-dark"]).toBe(vigilConsoleDarkTheme);
  });

  it("light variant: 浅灰底 + 冷青绿主色 + 方向 1 状态色", () => {
    expect(vigilConsoleTheme.palette.background.hex).toBe("#f3f4f6");
    expect(vigilConsoleTheme.palette.midground.hex).toBe("#009688");
    expect(vigilConsoleTheme.colorOverrides?.success).toBe("#22c55e");
    expect(vigilConsoleTheme.colorOverrides?.warning).toBe("#f59e0b");
    expect(vigilConsoleTheme.colorOverrides?.destructive).toBe("#ef4444");
    expect(vigilConsoleTheme.layout.radius).toBe("0.375rem"); // 6px
  });

  it("uses Inter + JetBrains Mono typography", () => {
    expect(vigilConsoleTheme.typography.fontSans).toContain("Inter");
    expect(vigilConsoleTheme.typography.fontMono).toContain("JetBrains Mono");
    expect(vigilConsoleTheme.typography.fontUrl).toBeTruthy();
  });

  it("dark variant: 青绿暗色", () => {
    expect(vigilConsoleDarkTheme.name).toBe("vigil-console-dark");
    expect(vigilConsoleDarkTheme.palette.background.hex).toBe("#0f1518");
    expect(vigilConsoleDarkTheme.palette.midground.hex).toBe("#26a69a");
  });

  it("legacy default kept as optional theme (Vigil Blue-Grey Legacy)", () => {
    expect(defaultTheme.name).toBe("default");
    expect(defaultTheme.label).toContain("Legacy");
  });
});
