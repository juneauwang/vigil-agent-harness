#!/usr/bin/env python3
"""Ops profile initializer (Ops Agent Harness, ops-agent-harness.md).

Creates the ``ops`` profile, writes the ops config (topo toolset + TOPO
memory provider + permission matrix), and seeds the sample topology table
(``ops-profile/topology.yaml`` + ``ops-profile/entities/``) into the
profile's HERMES_HOME.

Usage:
    python3 scripts/ops_init.py [--root PATH] [--env test|uat|prod]
                                [--force] [--no-alias]

Files written (inside the Hermes root, default ``~/.hermes`` or $HERMES_HOME):
    <root>/profiles/ops/config.yaml
    <root>/profiles/ops/topology.yaml
    <root>/profiles/ops/entities/*.yaml
    <root>/profiles/ops/runbooks/*.yaml

Idempotent: an existing profile / config.yaml / topology.yaml is left
untouched unless ``--force`` is passed.  Nothing outside the Hermes root is
written except the optional ops wrapper alias (~/.local/bin).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SAMPLE_DIR = _REPO_ROOT / "ops-profile"
_PROFILE_NAME = "ops"

# ops 配置 = 一期交付（拓扑注入 A + topo 工具 B + 权限矩阵 D）的全部激活开关。
# 见 ops-agent-harness.md §3/§4 与 OPS-DELTA.md。
_CONFIG_TPL = """\
# ops profile —— 由 scripts/ops_init.py 生成。
# 行为配置一律走 config.yaml（upstream 约定：不加 HERMES_* env var）。
#
# 核对通过、准备接管生产前，把 ops.permissions.env 改为 prod：
#   - L2（重启服务/装包）→ 审批；L3（rm -rf/重启DB/改配置）→ 拒绝；L4（删namespace/删库）→ 拒绝
#   - 跨环境操作默认拒绝；strict 环境操作需审批（ops-agent-harness.md §3）
_config_version: {version}
platform_toolsets:
  cli: [hermes-cli, topo, runbook]
tools:
  tool_search:
    enabled: off
    # 必须关闭 Tool Search：auto 会把非核心工具换成 tool_search/tool_describe/tool_call
    # 三个桥接工具，topo_* 被延后、不出现在 schema，topo_query/topo_update 将不可用。
memory:
  provider: topo
ops:
  topology:
    enabled: true
  runbooks:
    enabled: true
  permissions:
    enabled: true
    env: {env}
    role: {env}
"""


def _resolve_root(arg_root: str | None) -> Path:
    """Hermes root for profiles: --root wins, else env/default resolution."""
    if arg_root:
        root = Path(arg_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root
    sys.path.insert(0, str(_REPO_ROOT))
    from hermes_constants import get_default_hermes_root
    return Path(get_default_hermes_root())


def _latest_config_version() -> int:
    sys.path.insert(0, str(_REPO_ROOT))
    from hermes_cli.config import DEFAULT_CONFIG
    return int(DEFAULT_CONFIG.get("_config_version") or 1)


def _write_config(profile_dir: Path, env: str, force: bool) -> bool:
    """Write config.yaml into the profile. Returns True when written."""
    path = profile_dir / "config.yaml"
    if path.exists() and not force:
        print(f"· config.yaml 已存在，跳过（--force 覆盖）：{path}")
        return False
    text = _CONFIG_TPL.format(version=_latest_config_version(), env=env)
    path.write_text(text, encoding="utf-8")
    print(f"· 写入 config.yaml：{path}")
    return True


def _seed_samples(profile_dir: Path, force: bool) -> bool:
    """Copy the sample topology table + runbooks into the profile. Returns True if copied."""
    src = _SAMPLE_DIR / "topology.yaml"
    if not src.is_file():
        raise SystemExit(f"缺少样例拓扑 {src} —— 初始化中止。")
    dst = profile_dir / "topology.yaml"
    if dst.exists() and not force:
        print(f"· topology.yaml 已存在，跳过（--force 覆盖）：{dst}")
    else:
        shutil.copy2(src, dst)
        print(f"· 写入 topology.yaml：{dst}")

    entities_src = _SAMPLE_DIR / "entities"
    entities_dst = profile_dir / "entities"
    if not entities_src.is_dir():
        raise SystemExit(f"缺少样例实体目录 {entities_src} —— 初始化中止。")

    if not force and entities_dst.exists() and any(entities_dst.iterdir()):
        print(f"· entities/ 已存在且非空，跳过（--force 覆盖）：{entities_dst}")
        return True

    entities_dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src_file in sorted(entities_src.glob("*.yaml")):
        shutil.copy2(src_file, entities_dst / src_file.name)
        copied += 1
    print(f"· 写入 entities/：{copied} 个实体档案 → {entities_dst}")

    runbooks_src = _SAMPLE_DIR / "runbooks"
    runbooks_dst = profile_dir / "runbooks"
    if not runbooks_src.is_dir():
        raise SystemExit(f"缺少样例 runbooks 目录 {runbooks_src} —— 初始化中止。")
    if not force and runbooks_dst.exists() and any(runbooks_dst.iterdir()):
        print(f"· runbooks/ 已存在且非空，跳过（--force 覆盖）：{runbooks_dst}")
        return True
    runbooks_dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src_file in sorted(runbooks_src.glob("*.yaml")):
        shutil.copy2(src_file, runbooks_dst / src_file.name)
        copied += 1
    print(f"· 写入 runbooks/：{copied} 个 runbook → {runbooks_dst}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        help="Hermes 根目录（默认 ~/.hermes 或 $HERMES_HOME）；ops profile 建在 <root>/profiles/ops",
    )
    parser.add_argument(
        "--env", choices=("test", "uat", "prod"), default="test",
        help="ops.permissions.env 初始环境（默认 test，安全默认；验证通过后切 prod）",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="覆盖已存在的 config.yaml / topology.yaml / entities/（不触碰 .env、会话等用户数据）",
    )
    parser.add_argument(
        "--no-alias", action="store_true",
        help="不创建 ops 包装命令（仅 argus -p ops 可用）",
    )
    args = parser.parse_args()

    root = _resolve_root(args.root)
    sys.path.insert(0, str(_REPO_ROOT))
    os.environ["HERMES_HOME"] = str(root)  # 让 profile 解析锚定在 <root>/profiles

    from hermes_cli.profiles import (
        create_profile,
        create_wrapper_script,
        profile_exists,
    )

    profile_dir = root / "profiles" / _PROFILE_NAME
    if profile_exists(_PROFILE_NAME):
        print(f"· profile 'ops' 已存在：{profile_dir}")
    else:
        created = create_profile(
            _PROFILE_NAME,
            no_skills=True,          # ops profile 不装 bundled hermes skills，保持精简
            no_alias=True,           # 包装命令稍后单独创建（可控、可跳过）
            description="Ops Agent Harness 运维 profile（拓扑表 + topo 工具 + 权限矩阵）",
        )
        print(f"· 创建 profile 'ops'：{created}")

    _write_config(profile_dir, args.env, args.force)
    _seed_samples(profile_dir, args.force)

    if not args.no_alias:
        alias = create_wrapper_script(_PROFILE_NAME)
        if alias is None:
            print("· 未创建 ops 包装命令（argus 不在 PATH？）；可用 argus -p ops 代替")

    print("\n下一步（详见 OPS-VERIFY.md）：")
    print(f"  1. 核对拓扑：{profile_dir / 'topology.yaml'} 与 {profile_dir / 'entities'}")
    print(f"  2. 核对 runbook：{profile_dir / 'runbooks'}（事故处理 + L4 部署 checklist）")
    print(f"  3. 起 session：argus -p ops chat   （若 argus 不在 PATH，用 {_REPO_ROOT / 'hermes'} 代替）")
    print("  4. 验证 TOPO 段 / topo_query / topo_update / 权限矩阵 / runbook_load")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
