"""Ops profile initializer — E2E tests (scripts/ops_init.py + ops-profile sample).

Pins the §6.3 verification prerequisites for the ops harness:
  - init creates the ops profile, writes the ops config, seeds the topology
  - re-runs are idempotent; --force re-seeds
  - the seeded profile actually renders the TOPO block and serves topo_query
  - the sample topology stays a valid, <50-line layer-1 table
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import hermes_cli.config as hc
from plugins.memory.topo import render_topo_block
from tools.ops_permissions import check_ops_command_permission
from tools.runbook_tools import runbook_checkpoint, runbook_load
from tools.topo_tools import topo_query, topo_update

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INIT_SCRIPT = PROJECT_ROOT / "scripts" / "ops_init.py"
SAMPLE_DIR = PROJECT_ROOT / "hermes_cli" / "ops_samples"
SAMPLE_ENTITY_NAMES = sorted(p.stem for p in (SAMPLE_DIR / "entities").glob("*.yaml"))
SAMPLE_RUNBOOK_NAMES = sorted(p.stem for p in (SAMPLE_DIR / "runbooks").glob("*.yaml"))

ENTRIES = ["script", "module", "cli"]


def _run_init(root: Path, *extra: str, entry: str = "script") -> subprocess.CompletedProcess:
    """Run the ops initializer through one of three entry points:
    - script:  legacy ``scripts/ops_init.py`` shim
    - module:  packaged ``hermes_cli.ops_init`` module
    - cli:     ``vigil ops-init`` subcommand (hermes_cli.main dispatch)
    """
    if entry == "script":
        argv = [sys.executable, str(INIT_SCRIPT)]
    elif entry == "module":
        argv = [sys.executable, "-m", "hermes_cli.ops_init"]
    else:
        argv = [sys.executable, "-m", "hermes_cli.main", "ops-init"]
    return subprocess.run(
        [*argv, "--root", str(root), *extra],
        cwd=str(PROJECT_ROOT),
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture(params=ENTRIES)
def ops_home(tmp_path, monkeypatch, request):
    root = tmp_path / "hermes-root"
    entry = request.param
    proc = _run_init(root, "--no-alias", entry=entry)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    home = root / "profiles" / "ops"
    assert home.is_dir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        yield home
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


def _load_permissions(ops_home: Path) -> dict:
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        return hc.load_config_readonly()["ops"]["permissions"]
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


def test_init_creates_profile_config_and_topology(ops_home):
    assert (ops_home / "config.yaml").is_file()
    assert (ops_home / "topology.yaml").is_file()
    seeded = sorted(p.stem for p in (ops_home / "entities").glob("*.yaml"))
    assert seeded == SAMPLE_ENTITY_NAMES
    seeded_rb = sorted(p.stem for p in (ops_home / "runbooks").glob("*.yaml"))
    assert seeded_rb == SAMPLE_RUNBOOK_NAMES

    hc._LOAD_CONFIG_CACHE.clear()
    try:
        cfg = hc.load_config_readonly()
    finally:
        hc._LOAD_CONFIG_CACHE.clear()

    # 四个激活开关（A 注入 / B 工具 / D 矩阵）+ Tool Search 关闭（topo_* 不被延后）。
    assert cfg["platform_toolsets"]["cli"] == ["hermes-cli", "topo", "runbook"]
    assert cfg["memory"]["provider"] == "topo"
    assert cfg["ops"]["topology"]["enabled"] is True
    assert cfg["ops"]["runbooks"]["enabled"] is True
    perms = cfg["ops"]["permissions"]
    assert perms["enabled"] is True
    assert perms["env"] == "test" and perms["role"] == "test"  # 安全默认
    # ops.environments 默认三档（OPS-DELTA #11：env 可自定义的向后兼容基线）
    env_defs = cfg["ops"]["environments"]
    assert [d["name"] for d in env_defs] == ["test", "uat", "prod"]
    assert env_defs[0]["role"] == "test" and env_defs[2]["role"] == "prod"
    assert env_defs[1]["isolation"] == "strict" and env_defs[2]["isolation"] == "strict"
    # load_config_readonly 会把 "off" 规范化为 False；行为级校验看 ts_load()。
    assert cfg["tools"]["tool_search"]["enabled"] in ("off", False)

    from tools.tool_search import load_config as ts_load
    assert ts_load().enabled == "off"


@pytest.mark.parametrize("entry", ENTRIES)
def test_init_idempotent_and_force(tmp_path, entry):
    root = tmp_path / "hermes-root"
    proc = _run_init(root, "--no-alias", entry=entry)
    assert proc.returncode == 0
    home = root / "profiles" / "ops"
    cfg_path, topo_path = home / "config.yaml", home / "topology.yaml"
    runbooks_dir = home / "runbooks"

    # 用户改过的文件，重跑不覆盖。
    cfg_path.write_text("custom: true\n", encoding="utf-8")
    topo_path.write_text("custom: true\n", encoding="utf-8")
    (runbooks_dir / "mine.yaml").write_text("custom: true\n", encoding="utf-8")
    proc = _run_init(root, "--no-alias", entry=entry)
    assert proc.returncode == 0
    assert cfg_path.read_text(encoding="utf-8") == "custom: true\n"
    assert topo_path.read_text(encoding="utf-8") == "custom: true\n"
    assert (runbooks_dir / "mine.yaml").is_file()

    # --force 重新铺入样例（不触碰 .env / sessions 等用户数据）。
    proc = _run_init(root, "--no-alias", "--force", entry=entry)
    assert proc.returncode == 0
    assert yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["platform_toolsets"]["cli"] == ["hermes-cli", "topo", "runbook"]
    assert yaml.safe_load(topo_path.read_text(encoding="utf-8"))["version"] == 1
    # --force 重铺样例但不删除用户自建文件（mine.yaml 保留）。
    assert set(p.stem for p in runbooks_dir.glob("*.yaml")) >= set(SAMPLE_RUNBOOK_NAMES)
    assert (runbooks_dir / "mine.yaml").is_file()
    assert (home / ".env").is_file()


def test_seeded_profile_runbook_load(ops_home):
    listed = json.loads(runbook_load(home=ops_home))
    assert listed["count"] == len(SAMPLE_RUNBOOK_NAMES)

    result = json.loads(runbook_load(runbook="deploy-gateway-svc", home=ops_home))
    assert result["checklist"] is True
    assert [s["id"] for s in result["steps"]] == ["preflight", "deploy", "verify"]

    blocked = runbook_checkpoint(
        runbook="deploy-gateway-svc", step_id="deploy", status="pass", home=ops_home
    )
    assert "前置步骤未全部通过" in blocked


@pytest.mark.parametrize("entry", ENTRIES)
def test_env_flag_sets_permissions(tmp_path, monkeypatch, entry):
    root = tmp_path / "hermes-root"
    proc = _run_init(root, "--no-alias", "--env", "prod", entry=entry)
    assert proc.returncode == 0
    home = root / "profiles" / "ops"
    monkeypatch.setenv("HERMES_HOME", str(home))
    perms = _load_permissions(home)
    assert perms["env"] == "prod" and perms["role"] == "prod"


def test_env_flag_custom_env_defines_environments(tmp_path, monkeypatch):
    """--env bare_metal_prod：自定义环境名生成配置成功，且矩阵按 role=prod 判定。

    自定义名不再被 argparse choices 锁死（OPS-DELTA #11）；自动追加定义到
    ops.environments（isolation/role 推导，可在 config.yaml 调整）。
    """
    root = tmp_path / "hermes-root"
    proc = _run_init(root, "--no-alias", "--env", "bare_metal_prod", entry="module")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    home = root / "profiles" / "ops"
    monkeypatch.setenv("HERMES_HOME", str(home))
    perms = _load_permissions(home)
    assert perms["env"] == "bare_metal_prod"
    # 生成配置里 role 跟随环境定义（prod），不是环境名本身
    assert perms["role"] == "prod"

    hc._LOAD_CONFIG_CACHE.clear()
    try:
        cfg = hc.load_config_readonly()
    finally:
        hc._LOAD_CONFIG_CACHE.clear()
    env_defs = cfg["ops"]["environments"]
    names = [d["name"] for d in env_defs]
    assert names == ["test", "uat", "prod", "bare_metal_prod"]
    bmp = next(d for d in env_defs if d["name"] == "bare_metal_prod")
    assert bmp["role"] == "prod" and bmp["isolation"] == "strict"

    # 权限矩阵按自定义 env 名判定：bare_metal_prod → prod 档（L3 拒绝 / L2 审批）
    assert check_ops_command_permission("rm -rf /var/log")["action"] == "deny"
    assert check_ops_command_permission("rm -rf /var/log")["env"] == "bare_metal_prod"
    assert check_ops_command_permission("systemctl restart myapp")["action"] == "approve"


def test_env_flag_invalid_name_errors_listing_available(tmp_path):
    root = tmp_path / "hermes-root"
    proc = _run_init(root, "--no-alias", "--env", "Bad Env!", entry="module")
    assert proc.returncode == 2
    combined = proc.stdout + proc.stderr
    assert "无效环境名" in combined
    assert "test" in combined and "prod" in combined


def test_seeded_profile_renders_topo_and_queries(ops_home):
    block = render_topo_block(ops_home)
    assert block.startswith("## TOPO — 平台拓扑总览")
    assert "harbor" in block
    assert "ingress → gateway-svc → order-db" in block
    assert "执行任何运维操作前" in block

    harbor = json.loads(topo_query(entity="harbor", detail=True))
    assert harbor["name"] == "harbor"
    assert harbor["endpoint"] == "203.0.113.10:30443"
    assert harbor["stale"] is False
    assert harbor["detail"]["depends_on"] == ["postgres"]
    assert harbor["detail"]["ops"]["healthcheck"].startswith("curl")

    dbs = json.loads(topo_query(entity_type="db", env="prod"))
    assert {e["name"] for e in dbs["entities"]} == {"order-db", "postgres"}


def test_topo_update_test_entity_writes_source_agent(ops_home):
    result = json.loads(
        topo_update(entity="test-web", updates={"status": "degraded"}, reason="test")
    )
    assert result["source"] == "agent"
    assert result["env"] == "test"
    assert result["last_verified"] == _dt.date.today().isoformat()

    data = yaml.safe_load((ops_home / "entities" / "test-web.yaml").read_text(encoding="utf-8"))
    assert data["source"] == "agent"
    assert data["status"] == "degraded"
    assert data["last_verified"] == _dt.date.today().isoformat()


def test_default_env_test_does_not_block_l2(ops_home):
    # 安全默认（env=test）下矩阵放行常规操作，不会给新用户意外弹审批。
    assert check_ops_command_permission("systemctl restart myapp") is None


def test_sample_topology_stays_valid_and_under_50_lines():
    lines = (SAMPLE_DIR / "topology.yaml").read_text(encoding="utf-8").splitlines()
    assert len(lines) < 50, "第一层注入 system prompt，必须保持 <50 行"
    data = yaml.safe_load("\n".join(lines))
    assert data["version"] == 1
    assert {e["name"] for e in data["core_entities"]} == set(SAMPLE_ENTITY_NAMES)
    for ent in data["core_entities"]:
        assert (SAMPLE_DIR / ent["detail"]).is_file(), f"detail 指向缺失: {ent['detail']}"
    assert data["key_paths"] == [["ingress", "gateway-svc", "order-db"]]
