"""已非首装必需——全新安装的 default profile 自带 ops 配置与样例；本命令用于显式重建/迁移。

初始化 ops profile 的**数据层**（拓扑样例 + runbook 样例 + 配置文件）。运维能力
（topo/runbook 工具、权限矩阵）默认加载（OPS-DELTA #1/#38）：topo/runbook 按数据
存在性自动可用，权限矩阵默认启用；首装（ensure_hermes_home）已把同一份样例铺到
default profile 数据根——ops-init 保留给老用户/自托管显式重建 ops profile 或迁移。

Creates the ``ops`` profile, writes the ops config (topo toolset + TOPO
memory provider + permission matrix), and seeds the sample topology table
(``topology.yaml`` + ``hosts/`` + ``entities/``, schema v0.3 三层模型：
第一层总览 + 第二层服务索引 + 第三层详情档案) and sample runbooks
(``runbooks/``) into the profile's HERMES_HOME.

Shipped inside the wheel as ``vigil ops-init`` so pip-installed users can
initialize the ops profile without a repo checkout. The legacy
``scripts/ops_init.py`` entry point is a thin shim over this module.

Usage:
    vigil ops-init [--root PATH] [--env NAME] [--force] [--no-alias]

    --env 接受四值 local/test/dev/prod；老自定义名（uat/staging/bare_metal_prod/...）
    读取时按档位映射（uat→prod、staging→dev、其余按名字推导），映射打一次警告
    不报错——权限语义不放松，uat→prod 只会更严。

Files written (inside the Vigil root, default ``~/.vigil``; override with
``VIGIL_HOME`` / ``HERMES_HOME`` env or ``--root``):
    <root>/profiles/ops/config.yaml
    <root>/profiles/ops/topology.yaml
    <root>/profiles/ops/hosts/*.yaml
    <root>/profiles/ops/entities/*.yaml      # L3 命名：entities/{cluster}__{host}__{name}.yaml
    <root>/profiles/ops/runbooks/*.yaml

Idempotent: an existing profile / config.yaml / topology.yaml is left
untouched unless ``--force`` is passed.  Nothing outside the Vigil root is
written except the optional ops wrapper alias (~/.local/bin).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

_SAMPLE_DIR = Path(__file__).resolve().parent / "ops_samples"
_PROFILE_NAME = "ops"

# ops 配置 = 一期交付（拓扑注入 A + topo 工具 B + 权限矩阵 D）。
# 能力默认加载（OPS-DELTA #1）：topo/runbook 按数据存在性、矩阵默认启用；
# 下面的 enabled: true 是显式声明（向后兼容），显式 false 仍可关闭。
_CONFIG_TPL = """\
# ops profile —— 由 vigil ops-init 生成。
# 行为配置一律走 config.yaml（upstream 约定：不加 HERMES_* env var）。
#
# 核对通过、准备接管生产前，把 ops.permissions.env 改为 prod：
#   - L2（重启服务/装包）→ 审批；L3（rm -rf/重启DB/改配置）→ 拒绝；L4（删namespace/删库）→ 拒绝
#   - 跨环境操作默认拒绝；strict 环境操作需审批（ops-agent-harness.md §3）
_config_version: {version}
display:
  # ops 主题：vigil（蓝灰系内置皮肤，见 hermes_cli/skin_engine.py）
  skin: vigil
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
  # 环境定义列表（OPS-DELTA #42）：四值枚举 local/test/dev/prod（local/test/dev
  # relaxed、prod strict）。老自定义名（uat/staging/...）读取时按档位映射
  # （uat→prod 档，权限只会更严不会更松），不追加自定义定义。
  # 这是 /env 命令的可用名单；topology.yaml 的 environments 段须与这里同源，
  # 不一致以 config 为准。
  environments:
{environments}
  topology:
    enabled: true
  runbooks:
    enabled: true
  prometheus:
    endpoint: ""        # 如 http://127.0.0.1:9090；空 = prom_query 不可用
    alertmanager: ""    # 如 http://127.0.0.1:9093；空 = alert_query 不可用
    vault_path: ""      # 可选：本机保险箱 JSON 凭据条目 {{"user": ..., "pass": ...}}
  watch:
    enabled: false      # 值守采集（vigil watch install 常驻服务）：显式 true + alertmanager 配置后才采集
  permissions:
    enabled: true
    env: {env}
    role: {role}
"""


def _resolve_root(arg_root: str | None) -> Path:
    """Vigil root for profiles: --root wins, else env/default resolution."""
    if arg_root:
        root = Path(arg_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root
    from hermes_constants import get_default_hermes_root
    return Path(get_default_hermes_root())


def _latest_config_version() -> int:
    from hermes_cli.config import DEFAULT_CONFIG
    return int(DEFAULT_CONFIG.get("_config_version") or 1)


_DEFAULT_ENV_DEFS = [
    {"name": "local", "isolation": "relaxed", "role": "local"},
    {"name": "test", "isolation": "relaxed", "role": "test"},
    {"name": "dev", "isolation": "relaxed", "role": "dev"},
    {"name": "prod", "isolation": "strict", "role": "prod"},
]
_ENV_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ENV_TIERS = ("local", "test", "dev", "prod")
_LEGACY_ENV_TIER_MAP = {"uat": "prod", "staging": "dev"}


def _env_tier(env: str) -> str:
    """环境名 → 四值档位（写入端映射，权限语义不放松）。

    uat → prod 档、staging → dev 档；其余按名字推导（含 prod → prod、
    尾缀 local/test/dev → 对应档、默认 dev）。读取端映射（带警告）在
    tools/ops_permissions.py。
    """
    lower = str(env or "").strip().lower()
    if lower in _ENV_TIERS:
        return lower
    if lower in _LEGACY_ENV_TIER_MAP:
        return _LEGACY_ENV_TIER_MAP[lower]
    if "prod" in lower:
        return "prod"
    if lower.endswith("local"):
        return "local"
    if lower.endswith("test"):
        return "test"
    if lower.endswith("dev"):
        return "dev"
    return "dev"


def _resolve_env_defs(env: str) -> list[dict]:
    """四值档位定义；老自定义名按档位映射（打一次警告，不报错——不打断老数据）。

    环境枚举固定为 local/test/dev/prod（OPS-DELTA #42）：uat 移除、加 local/dev。
    老自定义名（uat/staging/bare_metal_prod/...）读取时映射到最近档位——
    uat → prod 档（权限只会更严不会更松）、staging → dev 档、其余按名字推导。
    """
    tier = _env_tier(env)
    if tier != str(env or "").strip().lower():
        print(f"· 提示：自定义环境 {env} 已映射到 {tier} 档"
              f"（环境枚举现为 {'/'.join(_ENV_TIERS)}；权限语义不放松，"
              f"uat→prod 只会更严）。如需调整请在 config.yaml ops.environments 修改")
    return [dict(d) for d in _DEFAULT_ENV_DEFS]


def _environments_yaml(env_defs: list[dict]) -> str:
    return "\n".join(
        f"    - name: {d['name']}\n      isolation: {d['isolation']}\n      role: {d['role']}"
        for d in env_defs
    )


def _write_config(profile_dir: Path, env: str, force: bool) -> bool:
    """Write config.yaml into the profile. Returns True when written."""
    path = profile_dir / "config.yaml"
    if path.exists() and not force:
        print(f"· config.yaml 已存在，跳过（--force 覆盖）：{path}")
        return False
    env_defs = _resolve_env_defs(env)
    tier = _env_tier(env)
    role = next(
        (d["role"] for d in env_defs if d["name"] == tier), tier
    )
    text = _CONFIG_TPL.format(
        version=_latest_config_version(), env=tier, role=role,
        environments=_environments_yaml(env_defs),
    )
    path.write_text(text, encoding="utf-8")
    print(f"· 写入 config.yaml：{path}")
    return True


def seed_ops_samples(dst_dir: Path, *, force: bool = False, report=None) -> bool:
    """Copy the sample topology table + runbooks into ``dst_dir`` (idempotent).

    Shared by ``vigil ops-init``（铺到 profile 目录）和首装铺设（OPS-DELTA
    #38：ensure_hermes_home 把同一份样例铺到 default profile 数据根），避免
    两份样例拷贝漂移。已存在的目标文件/非空目录一律跳过，除非 ``force``。
    ``report`` 为可选的可调用对象（接收 str 进度消息）；缺省静默（首装路径
    由 ensure_hermes_home 调用，不向 stdout 输出）。
    """
    _report = report or (lambda _m: None)
    src = _SAMPLE_DIR / "topology.yaml"
    if not src.is_file():
        raise SystemExit(f"缺少样例拓扑 {src} —— 初始化中止。")
    dst = dst_dir / "topology.yaml"
    if dst.exists() and not force:
        _report(f"· topology.yaml 已存在，跳过（--force 覆盖）：{dst}")
    else:
        shutil.copy2(src, dst)
        _report(f"· 写入 topology.yaml：{dst}")

    entities_src = _SAMPLE_DIR / "entities"
    entities_dst = dst_dir / "entities"
    if not entities_src.is_dir():
        raise SystemExit(f"缺少样例实体目录 {entities_src} —— 初始化中止。")

    if not force and entities_dst.exists() and any(entities_dst.iterdir()):
        _report(f"· entities/ 已存在且非空，跳过（--force 覆盖）：{entities_dst}")
    else:
        entities_dst.mkdir(parents=True, exist_ok=True)
        copied = 0
        for src_file in sorted(entities_src.glob("*.yaml")):
            shutil.copy2(src_file, entities_dst / src_file.name)
            copied += 1
        _report(f"· 写入 entities/：{copied} 个实体档案 → {entities_dst}")

    hosts_src = _SAMPLE_DIR / "hosts"
    hosts_dst = dst_dir / "hosts"
    if hosts_src.is_dir():
        if not force and hosts_dst.exists() and any(hosts_dst.iterdir()):
            _report(f"· hosts/ 已存在且非空，跳过（--force 覆盖）：{hosts_dst}")
        else:
            hosts_dst.mkdir(parents=True, exist_ok=True)
            copied_hosts = 0
            for src_file in sorted(hosts_src.glob("*.yaml")):
                shutil.copy2(src_file, hosts_dst / src_file.name)
                copied_hosts += 1
            _report(f"· 写入 hosts/：{copied_hosts} 个服务索引 → {hosts_dst}")

    runbooks_src = _SAMPLE_DIR / "runbooks"
    runbooks_dst = dst_dir / "runbooks"
    if not runbooks_src.is_dir():
        raise SystemExit(f"缺少样例 runbooks 目录 {runbooks_src} —— 初始化中止。")
    if not force and runbooks_dst.exists() and any(runbooks_dst.iterdir()):
        _report(f"· runbooks/ 已存在且非空，跳过（--force 覆盖）：{runbooks_dst}")
    else:
        runbooks_dst.mkdir(parents=True, exist_ok=True)
        copied = 0
        for src_file in sorted(runbooks_src.glob("*.yaml")):
            shutil.copy2(src_file, runbooks_dst / src_file.name)
            copied += 1
        _report(f"· 写入 runbooks/：{copied} 个 runbook → {runbooks_dst}")
    return True


def _seed_samples(profile_dir: Path, force: bool) -> bool:
    """Legacy wrapper — real logic lives in :func:`seed_ops_samples` (shared with first-run install)."""
    return seed_ops_samples(profile_dir, force=force, report=print)


def _warn_topology_env_sync(profile_dir: Path, env: str) -> None:
    """拓扑 environments 段与 config 的 env 定义同源（不一致以 config 为准）。

    ops.environments 是权限矩阵与 /env 的权威名单；topology.yaml 的 environments
    段是注入 system prompt 的展示层。两者不一致时告警（config 为准），提醒补拓扑，
    不阻断初始化。
    """
    topo_path = profile_dir / "topology.yaml"
    if not topo_path.is_file():
        return
    try:
        import yaml
        data = yaml.safe_load(topo_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    topo_envs = {
        str(e.get("name") or "").strip().lower()
        for e in (data.get("environments") or []) if isinstance(e, dict)
    }
    tier = _env_tier(env)
    if tier not in topo_envs:
        print(f"· 提示：当前环境档位 {tier} 不在 {topo_path.name} 的 environments 段"
              f"（{', '.join(sorted(topo_envs)) or '无'}）。权限矩阵按 config "
              f"ops.environments 生效（以 config 为准）；如需拓扑展示该档位，请补定义")


def run(root: Path, env: str = "test", force: bool = False, no_alias: bool = False) -> int:
    """Initialize the ops profile under ``root``. Returns process exit code."""
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
            no_skills=True,          # ops profile 不装 bundled skills，保持精简
            no_alias=True,           # 包装命令稍后单独创建（可控、可跳过）
            description="Ops Agent Harness 运维 profile（拓扑表 + topo 工具 + 权限矩阵）",
        )
        print(f"· 创建 profile 'ops'：{created}")

    _write_config(profile_dir, env, force)
    _seed_samples(profile_dir, force)
    _warn_topology_env_sync(profile_dir, env)

    if not no_alias:
        alias = create_wrapper_script(_PROFILE_NAME)
        if alias is None:
            print("· 未创建 ops 包装命令（vigil 不在 PATH？）；可用 vigil -p ops 代替")

    print("\n下一步（详见 OPS-VERIFY.md）：")
    print(f"  1. 核对拓扑：{profile_dir / 'topology.yaml'}（第一层）+ "
          f"{profile_dir / 'hosts'}（第二层服务索引）+ {profile_dir / 'entities'}（第三层详情）")
    print(f"  2. 核对 runbook：{profile_dir / 'runbooks'}（事故处理 + L4 部署 checklist）")
    print("  3. 起 session：vigil -p ops chat")
    print("  4. 验证 TOPO 段 / topo_query / topo_update / 权限矩阵 / runbook_load")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vigil ops-init",
        description=__doc__,
    )
    parser.add_argument(
        "--root",
        help="Vigil 根目录（默认 ~/.vigil，或 $VIGIL_HOME/$HERMES_HOME）；ops profile 建在 <root>/profiles/ops",
    )
    parser.add_argument(
        "--env", default="test",
        help="ops.permissions.env 初始环境（默认 test，安全默认；四值 "
             "local/test/dev/prod；老自定义名如 uat/staging 按档位映射）",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="覆盖已存在的 config.yaml / topology.yaml / entities/（不触碰 .env、会话等用户数据）",
    )
    parser.add_argument(
        "--no-alias", action="store_true",
        help="不创建 ops 包装命令（仅 vigil -p ops 可用）",
    )
    args = parser.parse_args(argv)
    env = args.env.strip().lower() if args.env else ""
    if not env or not _ENV_NAME_RE.fullmatch(env):
        available = ", ".join(d["name"] for d in _DEFAULT_ENV_DEFS)
        print(f"✗ 无效环境名 {args.env!r}：仅支持小写字母/数字/下划线/连字符"
              f"（四值 {available}；老自定义名如 uat/staging 按档位映射，无需注册）")
        return 2
    return run(_resolve_root(args.root), env=env, force=args.force, no_alias=args.no_alias)


if __name__ == "__main__":
    raise SystemExit(main())
