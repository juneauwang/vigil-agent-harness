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
    env = dict(os.environ)
    # 钉住本仓库：script 入口的 sys.path[0] 是 scripts/，editable 安装可能把
    # hermes_cli 解析到别处——本测试断言的是 PROJECT_ROOT 的样例与代码。
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if entry == "script":
        argv = [sys.executable, str(INIT_SCRIPT)]
    elif entry == "module":
        argv = [sys.executable, "-m", "hermes_cli.ops_init"]
    else:
        argv = [sys.executable, "-m", "hermes_cli.main", "ops-init"]
    return subprocess.run(
        [*argv, "--root", str(root), *extra],
        cwd=str(PROJECT_ROOT),
        env=env,
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
    monkeypatch.setenv("VIGIL_HOME", str(home))
    _write_test_matrix(home)
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        yield home
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


def _write_test_matrix(home: Path) -> None:
    """写测试矩阵：template2 + test 行（restart/query 等 execute）——P5 后
    权限判定 = 动作枚举 × 矩阵；空矩阵默认全 approve 保守。"""
    from tools import matrix_data as _md
    doc = _md.template_matrix("template2")
    matrix = doc["matrix"]
    matrix["test"] = {
        "query": "execute", "fetch_log": "execute", "verify": "execute",
        "restart": "execute", "start": "execute", "stop": "execute",
        "reload": "execute", "deploy": "execute", "run_script": "execute",
    }
    _md.write_matrix({
        "schema_version": 1,
        "source": "template2",
        "base_template": "template2",
        "matrix": matrix,
        "sources": {env: {act: "template2" for act in cells}
                    for env, cells in matrix.items()},
    }, home)


def _load_permissions(ops_home: Path) -> dict:
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        return hc.load_config_readonly()["ops"]["permissions"]
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


def test_init_creates_profile_config_and_topology(ops_home):
    assert (ops_home / "config.yaml").is_file()
    assert (ops_home / "topology.yaml").is_file()
    # v0.4 四层样例：第一层 topology.yaml + 第二层 services/ + 第三层 entities/ + 硬件层。
    seeded_services = sorted(p.stem for p in (ops_home / "services").glob("*.yaml"))
    assert seeded_services == sorted(p.stem for p in (SAMPLE_DIR / "services").glob("*.yaml"))
    seeded_hw = sorted(p.stem for p in (ops_home / "hardware").glob("*.yaml"))
    assert seeded_hw == sorted(p.stem for p in (SAMPLE_DIR / "hardware").glob("*.yaml"))
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
    # ops.environments 四值枚举（OPS-DELTA #42：local/test/dev relaxed、prod strict）
    env_defs = cfg["ops"]["environments"]
    assert [d["name"] for d in env_defs] == ["local", "test", "dev", "prod"]
    assert env_defs[0]["role"] == "local" and env_defs[3]["role"] == "prod"
    assert env_defs[0]["isolation"] == "relaxed" and env_defs[3]["isolation"] == "strict"
    assert env_defs[2]["isolation"] == "relaxed"  # dev relaxed
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
    assert yaml.safe_load(topo_path.read_text(encoding="utf-8"))["version"] == 4
    assert (home / "services" / "node1.yaml").is_file()
    assert (home / "hardware" / "node1.yaml").is_file()
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
    monkeypatch.setenv("VIGIL_HOME", str(home))
    perms = _load_permissions(home)
    assert perms["env"] == "prod" and perms["role"] == "prod"


def test_env_flag_custom_env_maps_to_tier(tmp_path, monkeypatch):
    """--env bare_metal_prod：老自定义名按档位映射到 prod（OPS-DELTA #42）。

    写入端只产四值枚举：不再追加自定义定义；ops.permissions.env 落映射后的
    prod 档，role=prod，权限矩阵按 prod 档判定（L3 拒绝 / L2 审批）。
    """
    root = tmp_path / "hermes-root"
    proc = _run_init(root, "--no-alias", "--env", "bare_metal_prod", entry="module")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "映射" in proc.stdout + proc.stderr           # 映射打一次警告（提示）
    home = root / "profiles" / "ops"
    monkeypatch.setenv("VIGIL_HOME", str(home))
    perms = _load_permissions(home)
    assert perms["env"] == "prod"                        # 映射后的档位，不是原名
    assert perms["role"] == "prod"

    hc._LOAD_CONFIG_CACHE.clear()
    try:
        cfg = hc.load_config_readonly()
    finally:
        hc._LOAD_CONFIG_CACHE.clear()
    env_defs = cfg["ops"]["environments"]
    assert [d["name"] for d in env_defs] == ["local", "test", "dev", "prod"]
    prod = next(d for d in env_defs if d["name"] == "prod")
    assert prod["role"] == "prod" and prod["isolation"] == "strict"

    # 操作矩阵按映射后的档位判定（YAPL P5：unknown → 默认 approve；restart →
    # prod 矩阵 required）。home 无 matrix.yaml → 空矩阵默认 approve 保守。
    decision = check_ops_command_permission("rm -rf /var/log")
    assert decision is not None and decision["action"] == "approve"
    decision = check_ops_command_permission("systemctl restart myapp")
    assert decision is not None and decision["action"] == "approve"


def test_env_flag_legacy_uat_maps_prod_tier(tmp_path, monkeypatch):
    """--env uat：老自定义名 → prod 档（更严不更松），写入端只产四值。"""
    root = tmp_path / "hermes-root"
    proc = _run_init(root, "--no-alias", "--env", "uat", entry="module")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "映射" in proc.stdout + proc.stderr
    home = root / "profiles" / "ops"
    monkeypatch.setenv("VIGIL_HOME", str(home))
    perms = _load_permissions(home)
    assert perms["env"] == "prod" and perms["role"] == "prod"
    # 权限判定 = prod 档语义（YAPL P5：unknown → 默认 approve；restart → 矩阵
    # 判定，env_tier 映射 prod）。
    unknown = check_ops_command_permission("rm -rf /var/log")
    assert unknown is not None and unknown["action"] == "approve" and unknown["env_tier"] == "prod"
    approve = check_ops_command_permission("systemctl restart myapp")
    assert approve is not None and approve["action"] == "approve" and approve["env_tier"] == "prod"


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
    # v0.4：第一层只注入 clusters + hosts（服务在第二层，不进 system prompt）。
    assert "node1" in block and "k3s-prod" in block and "test-host" in block
    assert "执行任何运维操作前" in block

    harbor = json.loads(topo_query(entity="harbor", detail=True))
    assert harbor["name"] == "harbor"
    assert harbor["endpoint"] == "203.0.113.10:30443"
    assert harbor["stale"] is False
    # v0.4：depends_on 上移第二层（entity 查询顶层即 L2 行）；checks 结构化档案。
    assert harbor["depends_on"] == ["postgres"]
    assert harbor["detail"]["checks"][0]["action"] == "verify"
    assert harbor["detail"]["checks"][0]["params"]["expect"]["http_status"] == 200

    dbs = json.loads(topo_query(entity_type="db", env="prod"))
    assert {e["name"] for e in dbs["entities"]} == {"order-db", "postgres"}

    node1 = json.loads(topo_query(host="node1"))
    assert node1["name"] == "node1"
    assert node1["role"] == ["control-plane", "worker"] and node1["runtime"] == ["k3s"]
    assert {s["name"] for s in node1["services"]} == {"harbor", "argocd", "order-db", "postgres"}


def test_topo_update_test_entity_writes_source_agent(ops_home):
    result = json.loads(
        topo_update(entity="test-web", updates={"status": "degraded"}, reason="test")
    )
    assert result["source"] == "agent"
    assert result["env"] == "test"
    assert result["last_verified"] == _dt.date.today().isoformat()

    # L3 命名：entities/{cluster}__{host}__{name}.yaml（test-host cluster=default）。
    data = yaml.safe_load(
        (ops_home / "entities" / "default__test-host__test-web.yaml").read_text(encoding="utf-8")
    )
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
    # v0.4 四层：clusters（type 即 controller）+ hosts（role/runtime 数组）+ 无旧字段
    # → 扁平实体集 == entities/ 第三层档案集（样例完整性契约）。
    assert data["version"] == 4
    for gone in ("sources", "services_index", "cross_host", "key_paths"):
        assert gone not in data
    hosts = data["hosts"]
    assert {h["name"] for h in hosts} == {"node1", "node2", "test-host"}
    for host in hosts:
        assert host["type"] == "host"
        assert isinstance(host["role"], list) and isinstance(host["runtime"], list)
        assert "services_index" not in host
    clusters = data.get("clusters") or []
    assert {c["name"] for c in clusters} == {"k3s-prod"}
    assert clusters[0]["type"] in ("k8s", "k3s", "kind", "docker", "bare")
    assert clusters[0]["provenance"] in ("terraform", "ansible", "salt", "manual")
    assert all(c["env"] in ("local", "test", "dev", "prod") for c in clusters)
    assert {e["name"] for e in data["environments"]} == {"local", "test", "dev", "prod"}
    flat = set()
    detail_entities = set()
    for host in hosts:
        index = yaml.safe_load(
            (SAMPLE_DIR / "services" / f"{host['name']}.yaml").read_text(encoding="utf-8")
        )
        assert index["host"] == host["name"]
        flat |= {s["name"] for s in index["services"]}
        detail_entities |= {s["name"] for s in index["services"]}
        host_cluster = host.get("cluster") or "default"
        for svc in index["services"]:
            assert (SAMPLE_DIR / svc["detail"]).is_file(), f"detail 指向缺失: {svc['detail']}"
            assert svc["detail"] == f"entities/{host_cluster}__{host['name']}__{svc['name']}.yaml", \
                f"L3 命名不符合规则: {svc['detail']}"
            # v0.4 服务行：无 env/cluster 冗余、type 受控枚举、managed_by 在枚举表。
            assert "env" not in svc and "cluster" not in svc
            assert svc["type"] in ("db", "cache", "queue", "registry", "monitor",
                                   "gateway", "search", "object_storage", "app", "unknown")
            assert svc["managed_by"] in ("docker", "docker_compose", "kubectl", "helm",
                                         "systemd", "pm2", "supervisord", "bare", "unknown")
        # 硬件层 hardware/<host>.yaml 存在（静态规格 + controller 枚举）。
        hw = yaml.safe_load(
            (SAMPLE_DIR / "hardware" / f"{host['name']}.yaml").read_text(encoding="utf-8")
        )
        assert hw["hardware"]["gpu"]["controller"] in (
            "nvidia-smi", "npu-smi", "cambricon-smi", "rocm-smi", "none")
        assert hw["network"]["firewall"]["controller"] in (
            "firewalld", "iptables", "ufw", "nftables", "none")
    # L3 文件名 = {cluster}__{host}__{name}.yaml：与扁平实体集按 name 部分比较。
    sample_names = {stem.rsplit("__", 1)[-1] for stem in SAMPLE_ENTITY_NAMES}
    assert flat == sample_names
    assert detail_entities == sample_names
