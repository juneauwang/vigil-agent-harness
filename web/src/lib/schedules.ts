/**
 * `/schedules` 的纯函数：从 cron job 的 `script_args` 里读写 digest 的
 * `--window`（页面与测试共用；放 lib 保持页面文件只导出组件）。
 */

/** `script_args` 里的 `--window` 值（没有 → 空串 → placeholder 7d）。 */
export function windowArg(args?: string[] | null): string {
  const list = args ?? [];
  const index = list.indexOf("--window");
  return index >= 0 && index + 1 < list.length ? list[index + 1] : "";
}

/**
 * 只替换/插入/删除 `--window` 这一个参数，其余参数原样保序。
 * 有值 → 覆盖（没有该参数就插到最前）；空值 → 去掉该参数（回落缺省窗口）。
 */
export function withWindowArg(args: readonly string[] | null | undefined, value: string): string[] {
  const base = Array.from(args ?? []);
  const next = value.trim();
  const out: string[] = [];
  let replaced = false;
  for (let i = 0; i < base.length; i += 1) {
    if (base[i] !== "--window") {
      out.push(base[i]);
      continue;
    }
    const hasValue = i + 1 < base.length && !base[i + 1].startsWith("--");
    if (hasValue) i += 1;
    if (next && !replaced) {
      out.push("--window", next);
      replaced = true;
    }
  }
  if (next && !replaced) out.unshift("--window", next);
  return out;
}

