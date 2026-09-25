#!/bin/sh
# =============================================================================
# git 护栏（取证仪器）—— 给测试全量回归用，**不是产品防护代码**
#
# ⚠️ 三声明（改动本文件前先读）：
#   ① 这是**测量仪器**，不是产品的一部分：它不进 conftest、不默认生效、
#      不写进任何会被产品 import 的路径。它是 `git` 的 PATH 前置包装，只在
#      你显式 `export PATH=<本目录>:$PATH` 的那次测量里起作用。
#   ② **正解永远是在 callsite 修掉触发源**，不是在护栏里加宽放行。用这个脚本
#      ≠ 允许不修 —— 它的价值是"把结论从噪声里救出来"，不是"替代码把风险挡掉"。
#   ③ 任何用它测出来的数字，**必须连带三件事一起报**：挂了没 / 护栏调用数 /
#      BLOCKED 数。缺任何一项，数字与"无护栏跑"的数字不可比。
#
# 作用：原样代理所有 git 调用；仅当「effective repo（-C 或 pwd -P）∈ PROTECT」
#   且子命令 ∈ checkout|switch|reset 时，打一行 stderr 并以 **exit 128** 拒绝。
# 为什么需要：tests/hermes_cli 里有一组测试会对当前 checkout 跑真
#   `git checkout main` / `git checkout -B main origin/main` / `git fetch` 循环。
#   没有护栏时整目录跑会慢到跑不完（实测：90% 处被 timeout 杀掉、F 计数 110），
#   且会把被测 worktree 的 HEAD 真的切走；带护栏时同样的套件能跑完、结果可判读
#   （实测：4 failed / 4932 passed / 23 skipped）。详见 skill 的
#   acceptance-pitfalls.md「测试套件会动真实 git 仓库」一节。
#
# 用法（四步，照抄）：
#   1) 装： cp scripts/git_guard_shim.sh /tmp/gitshim/git && chmod +x /tmp/gitshim/git
#   2) 配： export VIGIL_GUARD_PROTECT="<本次要跑的 worktree 路径> $(git rev-parse --show-toplevel)"
#          export VIGIL_GUARD_LOG=/tmp/gitshim.log && : > "$VIGIL_GUARD_LOG"
#          ⚠️ PROTECT 必须写成本次真正要跑的 checkout —— 护错路径 = 等于没护栏。
#   3) 冒烟（期望 exit=128）：
#          PATH=/tmp/gitshim:$PATH sh -c 'cd <worktree> && git checkout main'
#   4) 跑测 + 收工：
#          export PATH=/tmp/gitshim:$PATH && pytest tests/hermes_cli -q -p no:cacheprovider
#          grep -c BLOCKED "$VIGIL_GUARD_LOG"     # 0 = 本类触发源没出现；>0 = 见带 nodeid 的行
#          git -C <worktree> reflog -25           # 铁证：不该有 checkout: moving / reset: moving
#
# 环境变量：VIGIL_GUARD_PROTECT（必填，空格分隔绝对路径）、VIGIL_GUARD_LOG
#   （默认 /tmp/vigil-git-guard.log）、VIGIL_GUARD_REAL_GIT（默认 /usr/bin/git）。
#   PROTECT 为空时**拒绝工作并 exit 125**：空 PROTECT 与"没挂护栏"无法区分，
#   静默放行只会产出不可比的数字。
# =============================================================================
set -u

REAL_GIT="${VIGIL_GUARD_REAL_GIT:-/usr/bin/git}"
PROTECT="${VIGIL_GUARD_PROTECT:-}"
LOG="${VIGIL_GUARD_LOG:-/tmp/vigil-git-guard.log}"

if [ -z "$PROTECT" ]; then
  echo "git-guard: VIGIL_GUARD_PROTECT 为空，拒绝工作（空 PROTECT 与没挂护栏无法区分）。" >&2
  echo "git-guard: 先 export VIGIL_GUARD_PROTECT=\"<worktree 路径> <主检出路径>\" 再跑。" >&2
  exit 125
fi

dir=$(pwd -P)
prev=""
sub=""
for a in "$@"; do
  if [ "$prev" = "C" ]; then dir="$a"; prev=""; continue; fi
  if [ "$prev" = "c" ]; then prev=""; continue; fi
  case "$a" in
    -C) prev="C" ;;
    -C*) dir="${a#-C}" ;;
    -c) prev="c" ;;
    --git-dir=*|--work-tree=*) ;;
    -*) ;;
    *) [ -z "$sub" ] && sub="$a" ;;
  esac
done

printf '%s\t%s\t%s\t%s\n' "$(date +%s)" "$dir" "${PYTEST_CURRENT_TEST:-<none>}" "$*" >> "$LOG" 2>/dev/null

case "$sub" in
  checkout|switch|reset)
    for p in $PROTECT; do
      if [ "$dir" = "$p" ]; then
        printf 'BLOCKED\t%s\t%s\n' "$dir" "$*" >> "$LOG" 2>/dev/null
        echo "git-guard: refused destructive '$sub' against protected checkout $dir" >&2
        exit 128
      fi
    done
    ;;
esac

exec "$REAL_GIT" "$@"
