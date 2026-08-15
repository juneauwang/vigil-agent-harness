import type { DashboardTheme, ThemeTypography, ThemeLayout } from "./types";

/**
 * Built-in dashboard themes.
 *
 * Each theme defines its own palette, typography, and layout so switching
 * themes produces visible changes beyond just color — fonts, density, and
 * corner-radius all shift to match the theme's personality.
 *
 * Theme names must stay in sync with the backend's
 * `_BUILTIN_DASHBOARD_THEMES` list in `hermes_cli/web_server.py`.
 */

// ---------------------------------------------------------------------------
// Shared typography / layout presets
// ---------------------------------------------------------------------------

/** Default system stack — neutral, safe fallback for every platform. */
const SYSTEM_SANS =
  'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
const SYSTEM_MONO =
  'ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace';

const DEFAULT_TYPOGRAPHY: ThemeTypography = {
  fontSans: SYSTEM_SANS,
  fontMono: SYSTEM_MONO,
  baseSize: "15px",
  lineHeight: "1.55",
  letterSpacing: "0",
};

const DEFAULT_LAYOUT: ThemeLayout = {
  radius: "0.5rem",
  density: "comfortable",
};

// ---------------------------------------------------------------------------
// Themes
// ---------------------------------------------------------------------------

export const defaultTheme: DashboardTheme = {
  name: "default",
  label: "Vigil Blue-Grey (Legacy)",
  description: "Vigil 蓝灰运维主题——深灰蓝底 + 冷蓝光晕（旧默认，保留对比）",
  palette: {
    background: { hex: "#0b1220", alpha: 1 },
    midground: { hex: "#4A90D9", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(74, 144, 217, 0.35)",
    noiseOpacity: 0.8,
  },
  typography: DEFAULT_TYPOGRAPHY,
  layout: DEFAULT_LAYOUT,
  terminalBackground: "#000000",
};

// ---------------------------------------------------------------------------
// Vigil Console（方向 1：硬核工程师控制台，参考 Weave GitOps / Cilium Hubble）
//
// 底 #f6f8fa + 主色靛蓝 #2563eb + 终端深色底 #0f1c2d（豆包 HTML 布局骨架基准，
// GitLab 开源风 + 嵌入式终端融合）；状态色 绿 #22c55e / 琥珀 #f59e0b / 红 #ef4444 /
// 灰 #6b7280；1px 细边框 #e1e5eb、圆角 6px、仅悬浮抽屉/下拉极淡阴影；
// 界面 Inter、日志/命令 JetBrains Mono。暗色变体 #0f1c2d 系 + 主色亮化。
// ---------------------------------------------------------------------------

const VIGIL_CONSOLE_TYPOGRAPHY: ThemeTypography = {
  fontSans: `"Inter", ${SYSTEM_SANS}`,
  fontMono: `"JetBrains Mono", ${SYSTEM_MONO}`,
  fontUrl:
    "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap",
  baseSize: "14px",
  lineHeight: "1.5",
  letterSpacing: "-0.005em",
};

const VIGIL_CONSOLE_LAYOUT: ThemeLayout = {
  radius: "0.375rem", // 6px
  density: "comfortable",
};

export const vigilConsoleTheme: DashboardTheme = {
  name: "vigil-console",
  label: "Vigil Console",
  description: "浅灰底 #f6f8fa + 靛蓝 #2563eb — GitLab 开源风控制台（默认）",
  palette: {
    background: { hex: "#f6f8fa", alpha: 1 },
    midground: { hex: "#2563eb", alpha: 1 },
    foreground: { hex: "#1f2937", alpha: 1 },
    warmGlow: "rgba(37, 99, 235, 0.08)",
    noiseOpacity: 0,
  },
  typography: VIGIL_CONSOLE_TYPOGRAPHY,
  layout: VIGIL_CONSOLE_LAYOUT,
  layoutVariant: "tiled",
  colorOverrides: {
    primary: "#2563eb",
    primaryForeground: "#ffffff",
    destructive: "#ef4444",
    destructiveForeground: "#ffffff",
    success: "#22c55e",
    warning: "#f59e0b",
    border: "#e1e5eb",
    input: "#e1e5eb",
    ring: "#2563eb",
  },
  customCSS: `:root {
    --text-primary: #1f2937;
    --text-secondary: #4b5563;
    --text-tertiary: #6b7280;
    --text-disabled: #9ca3af;
    --text-on-accent: #ffffff;
    --text-display: #111827;
    --color-foreground: #1f2937;
    --color-background: #f6f8fa;
    --color-card: #ffffff;
    --color-card-foreground: #1f2937;
    --color-muted: #f1f3f6;
    --color-muted-foreground: #6b7280;
    --color-secondary: #eaeef3;
    --color-secondary-foreground: #1f2937;
    --color-popover: #ffffff;
    --color-popover-foreground: #1f2937;
    --color-accent: #e8effd;
    --color-accent-foreground: #1d4ed8;
    --color-destructive: #ef4444;
    --color-success: #22c55e;
    --color-warning: #f59e0b;
  }`,
  terminalBackground: "#0f1c2d",
  terminalForeground: "#d7e0ea",
  seriesColors: { inputTokenAccent: "#2563eb", outputTokenAccent: "#22c55e" },
  swatchColors: ["#f6f8fa", "#2563eb", "#22c55e"],
};

export const vigilConsoleDarkTheme: DashboardTheme = {
  name: "vigil-console-dark",
  label: "Vigil Console Dark",
  description: "Vigil Console 深色变体（#0f1c2d 系 + 主色亮化）",
  palette: {
    background: { hex: "#0f1c2d", alpha: 1 },
    midground: { hex: "#3b82f6", alpha: 1 },
    foreground: { hex: "#e5e7eb", alpha: 1 },
    warmGlow: "rgba(59, 130, 246, 0.16)",
    noiseOpacity: 0.12,
  },
  typography: VIGIL_CONSOLE_TYPOGRAPHY,
  layout: VIGIL_CONSOLE_LAYOUT,
  layoutVariant: "tiled",
  colorOverrides: {
    primary: "#3b82f6",
    primaryForeground: "#ffffff",
    destructive: "#ef4444",
    destructiveForeground: "#ffffff",
    success: "#22c55e",
    warning: "#f59e0b",
    border: "#24344a",
    input: "#24344a",
    ring: "#3b82f6",
  },
  customCSS: `:root {
    --text-primary: #e5e7eb;
    --text-secondary: #aab6c2;
    --text-tertiary: #8293a3;
    --text-disabled: #5e6e7e;
    --text-on-accent: #ffffff;
    --text-display: #f3f4f6;
    --color-foreground: #e5e7eb;
    --color-background: #0f1c2d;
    --color-card: #16263a;
    --color-card-foreground: #e5e7eb;
    --color-muted: #1c2e44;
    --color-muted-foreground: #9aa9b8;
    --color-secondary: #1f324a;
    --color-secondary-foreground: #e5e7eb;
    --color-popover: #16263a;
    --color-popover-foreground: #e5e7eb;
    --color-accent: #1c3a5f;
    --color-accent-foreground: #93c5fd;
    --color-destructive: #ef4444;
    --color-success: #22c55e;
    --color-warning: #f59e0b;
  }`,
  terminalBackground: "#0a1524",
  terminalForeground: "#c9d6e2",
  seriesColors: { inputTokenAccent: "#3b82f6", outputTokenAccent: "#22c55e" },
  swatchColors: ["#0f1c2d", "#3b82f6", "#22c55e"],
};

export const midnightTheme: DashboardTheme = {
  name: "midnight",
  label: "Midnight",
  description: "Deep blue-violet with cool accents",
  palette: {
    background: { hex: "#0a0a1f", alpha: 1 },
    midground: { hex: "#d4c8ff", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(167, 139, 250, 0.32)",
    noiseOpacity: 0.8,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Inter", ${SYSTEM_SANS}`,
    fontMono: `"JetBrains Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap",
    letterSpacing: "-0.005em",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0.75rem",
  },
};

export const emberTheme: DashboardTheme = {
  name: "ember",
  label: "Ember",
  description: "Warm crimson and bronze — forge vibes",
  palette: {
    background: { hex: "#1a0a06", alpha: 1 },
    midground: { hex: "#ffd8b0", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(249, 115, 22, 0.38)",
    noiseOpacity: 1,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Spectral", Georgia, "Times New Roman", serif`,
    fontMono: `"IBM Plex Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Spectral:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;700&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0.25rem",
  },
  colorOverrides: {
    destructive: "#c92d0f",
    warning: "#f97316",
  },
};

export const monoTheme: DashboardTheme = {
  name: "mono",
  label: "Mono",
  description: "Clean grayscale — minimal and focused",
  palette: {
    background: { hex: "#0e0e0e", alpha: 1 },
    midground: { hex: "#eaeaea", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(255, 255, 255, 0.1)",
    noiseOpacity: 0.6,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"IBM Plex Sans", ${SYSTEM_SANS}`,
    fontMono: `"IBM Plex Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0",
  },
};

export const cyberpunkTheme: DashboardTheme = {
  name: "cyberpunk",
  label: "Cyberpunk",
  description: "Neon green on black — matrix terminal",
  palette: {
    background: { hex: "#040608", alpha: 1 },
    midground: { hex: "#9bffcf", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(0, 255, 136, 0.22)",
    noiseOpacity: 1.2,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Share Tech Mono", "JetBrains Mono", ${SYSTEM_MONO}`,
    fontMono: `"Share Tech Mono", "JetBrains Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=JetBrains+Mono:wght@400;700&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0",
  },
  colorOverrides: {
    success: "#00ff88",
    warning: "#ffd700",
    destructive: "#ff0055",
  },
};

export const roseTheme: DashboardTheme = {
  name: "rose",
  label: "Rosé",
  description: "Soft pink and warm ivory — easy on the eyes",
  palette: {
    background: { hex: "#1a0f15", alpha: 1 },
    midground: { hex: "#ffd4e1", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(249, 168, 212, 0.3)",
    noiseOpacity: 0.9,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Fraunces", Georgia, serif`,
    fontMono: `"DM Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=DM+Mono:wght@400;500&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "1rem",
  },
};

/** Light mode — vivid Nous-blue accents on a cream canvas. */
export const nousBlueTheme: DashboardTheme = {
  name: "nous-blue",
  label: "Nous Blue",
  description: "Light mode — vivid Nous-blue accents on cream canvas",
  palette: {
    background: { hex: "#E8F2FD", alpha: 1 },
    midground: { hex: "#0053FD", alpha: 1 },
    foreground: { hex: "#170d02", alpha: 0 },
    warmGlow: "rgba(0, 83, 253, 0.12)",
    noiseOpacity: 0,
  },
  typography: DEFAULT_TYPOGRAPHY,
  layout: DEFAULT_LAYOUT,
  terminalBackground: "#f5f8fc",
  terminalForeground: "#170d02",
  seriesColors: {
    inputTokenAccent: "#001934",
    outputTokenAccent: "#0053fd",
  },
  swatchColors: ["#170d02", "#0053FD", "#E8F2FD"],
};

/**
 * Same look as ``defaultTheme`` but with a larger root font size, looser
 * line-height, and ``spacious`` density so every rem-based size in the
 * dashboard scales up. For users who find the default 15px UI too dense.
 */
export const defaultLargeTheme: DashboardTheme = {
  name: "default-large",
  label: "Vigil Blue-Grey (Large)",
  description: "Vigil Blue-Grey with bigger fonts and roomier spacing",
  palette: defaultTheme.palette,
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    baseSize: "18px",
    lineHeight: "1.65",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    density: "spacious",
  },
};

export const BUILTIN_THEMES: Record<string, DashboardTheme> = {
  default: defaultTheme,
  "default-large": defaultLargeTheme,
  "nous-blue": nousBlueTheme,
  midnight: midnightTheme,
  ember: emberTheme,
  mono: monoTheme,
  cyberpunk: cyberpunkTheme,
  rose: roseTheme,
  "vigil-console": vigilConsoleTheme,
  "vigil-console-dark": vigilConsoleDarkTheme,
};
