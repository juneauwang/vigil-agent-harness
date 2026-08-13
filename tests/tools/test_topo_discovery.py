"""OPS-DELTA #12 — 拓扑自动发现引擎 + topo-discover CLI 验收测试。

覆盖：mock ssh 输出（docker ps / compose ls / ss / nvidia-smi / kubectl）→
正确映射服务/端口/镜像、输出 v0.2 结构、source=discovered + needs_review=true；
凭据不落明文；dry-run 不写文件；确认落盘后 hosts/<hostname>.yaml 结构可被
topo_tools 校验；ssh 失败 → 明确错误无半截数据；v0.1 topology 拒绝覆盖；
--help 正常。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

import hermes_cli.config as hc
from tools.topo_discovery import (
    DiscoveryError,
    ProbeResult,
    discover_host,
    write_discovery,
)

DOCKER_PS = """\
{"Command":"/entrypoint.sh","ID":"abc123","Image":"harbor:v2.11.0","Labels":"com.docker.compose.project=harbor,com.docker.compose.service=harbor","Names":"harbor","Ports":"0.0.0.0:30443->5000/tcp, :::30443->5000/tcp","State":"running"}
{"Command":"docker-entrypoint.sh","ID":"def456","Image":"postgres:16-alpine","Labels":"com.docker.compose.project=db,com.docker.compose.service=postgres","Names":"db-postgres-1","Ports":"0.0.0.0:5432->5432/tcp","State":"running"}
"""
COMPOSE_LS = (
    '[{"Name":"harbor","Status":"running(1)","ConfigFiles":"/opt/harbor/compose.yaml"},'
    '{"Name":"db","Status":"running(1)","ConfigFiles":"/opt/db/compose.yaml"}]'
)
SS_TLNP = """\
State Recv-Q Send-Q Local Address:Port Peer Address:Port Process
LISTEN 0      128    0.0.0.0:22      0.0.0.0:*    users:(("sshd",pid=1,fd=3))
LISTEN 0      128    127.0.0.1:5432  0.0.0.0:*    users:(("postgres",pid=2,fd=4))
LISTEN 0      128    0.0.0.0:9090    0.0.0.0:*    users:(("prom",pid=3,fd=5))
"""
NVIDIA = "NVIDIA A100-SXM4-40GB, 40960 MiB"
KUBE = (
    '{"items":['
    '{"kind":"Service","metadata":{"name":"grafana","namespace":"monitoring"},'
    '"spec":{"ports":[{"port":3000,"nodePort":30030}]}},'
    '{"kind":"Deployment","metadata":{"name":"grafana","namespace":"monitoring"},'
    '"spec":{"template":{"spec":{"containers":[{"image":"grafana/grafana:10"}]}}}}'
    ']}'
)
SYSTEMCTL = """\
  UNIT                 LOAD   ACTIVE SUB     DESCRIPTION
  harbor.service        loaded active running Harbor systemd service
  node-exporter.service loaded active running Prometheus Node Exporter
  inactive.service      loaded inactive dead   Not discovered
"""


class FakeRunner:
    """可编程 mock runner：按命令前缀返回输出；记录调用（供明文断言）。"""

    def __init__(self, **probes):
        self.probes = probes
        self.calls: list = []

    def __call__(self, cmd: str) -> ProbeResult:
        self.calls.append(cmd)
        for key, value in self.probes.items():
            if key in cmd:
                if isinstance(value, Exception):
                    raise value
                if isinstance(value, ProbeResult):
                    return value
                if isinstance(value, tuple):
                    return ProbeResult(value[0], value[1], value[2] if len(value) > 2 else "")
                return ProbeResult(value)
        return ProbeResult("", 127)


def _default_runner(**overrides):
    probes = {
        "docker ps": DOCKER_PS,
        "compose ls": COMPOSE_LS,
        "kubectl": KUBE,
        "ss -tlnp": SS_TLNP,
        "nvidia-smi": NVIDIA,
    }
    probes.update(overrides)
    return FakeRunner(**probes)


# ---------------------------------------------------------------------------
# 发现引擎
# ---------------------------------------------------------------------------

def test_discover_maps_services_ports_images():
    runner = _default_runner()
    d = discover_host("203.0.113.20", "prod", runner=runner)

    assert d["version"] == 3
    assert d["source"] == "discovered"
    assert d["needs_review"] is True
    assert d["last_verified"]

    host = d["host"]
    assert host["name"] == "203.0.113.20"
    assert host["env"] == "prod"
    assert host["cluster"] == "default"          # 未标 cluster → 显示 default
    assert host["endpoint"] == "203.0.113.20"
    assert host["runtime"] == "docker"
    assert host["source"] == "discovered"
    assert host["needs_review"] is True
    assert host["attrs"]["gpu"] == ["NVIDIA A100-SXM4-40GB, 40960 MiB"]
    assert host["attrs"]["compose_projects"] == ["harbor", "db"]

    names = {s["name"]: s for s in d["services"]}
    assert "harbor" in names
    assert names["harbor"]["endpoint"] == "203.0.113.20:30443"
    assert names["harbor"]["attrs"]["image"] == "harbor:v2.11.0"
    assert names["harbor"]["attrs"]["ports"] == [30443]
    assert names["postgres"]["attrs"]["ports"] == [5432]
    # k8s 枚举：grafana svc（nodePort 30030）+ deploy（镜像）。
    assert names["grafana"]["type"] == "k8s-service"
    assert names["grafana"]["endpoint"] == "203.0.113.20:30030"
    # 端口扫描补条目：9090 未识别；22（sshd）与已映射端口不补。
    assert "unidentified-9090" in names
    assert "unidentified-22" not in names
    assert "unidentified-5432" not in names

    # 第三层详情草案：L3 命名 entities/{cluster}__{host}__{name}.yaml，
    # cluster 空用 env 兜底（本用例无 cluster → prod__host__name）。
    assert d["details"]["harbor"]["name"] == "harbor"
    assert d["details"]["harbor"]["needs_review"] is True
    assert d["details"]["harbor"]["cluster"] == "default"
    assert names["harbor"]["detail"] == "entities/prod__203.0.113.20__harbor.yaml"
    assert names["harbor"]["cluster"] == "default"
    assert names["grafana"]["detail"] == "entities/prod__203.0.113.20__grafana.yaml"
    assert names["unidentified-9090"]["detail"] == "entities/prod__203.0.113.20__unidentified-9090.yaml"


def test_discover_credentials_never_in_output_or_command_strings():
    runner = _default_runner()
    secret = "Sup3r-Secret!Password"
    d = discover_host("203.0.113.20", "prod", {"user": "root", "password": secret},
                      runner=runner)
    # 命令串（runner 记录）与输出/日志里不得出现密码明文。
    blob = json.dumps(d) + "\n" + "\n".join(runner.calls)
    assert secret not in blob
    # 服务/详情数据也不含凭据字段。
    assert "password" not in json.dumps(d).lower()


def test_entity_filename_no_collision_across_dimensions():
    from tools.topo_discovery import _entity_filename

    # 同 host 不同 app / 同 app 不同 host / 同 app 同 host 不同 cluster 全不冲突。
    a = _entity_filename("k3s-prod", "node1", "postgres")
    b = _entity_filename("k3s-prod", "node1", "harbor")
    c = _entity_filename("k3s-prod", "node2", "postgres")
    d = _entity_filename("k3s-a", "node1", "postgres")
    assert len({a, b, c, d}) == 4
    assert a == "entities/k3s-prod__node1__postgres.yaml"
    # cluster 空 → env 兜底；都空 → default。
    assert _entity_filename("", "node1", "postgres", "prod") == "entities/prod__node1__postgres.yaml"
    assert _entity_filename("", "node1", "postgres", "") == "entities/default__node1__postgres.yaml"


def test_entity_filename_sanitized_names_unambiguous():
    from tools.topo_discovery import _entity_filename

    # _sanitize_name 字符集（. _ -）保留；__ 分隔不歧义。
    assert _entity_filename("prod", "node.a-1", "my_app-v2") == "entities/prod__node.a-1__my_app-v2.yaml"
    assert _entity_filename("Prod Cluster", "Node.1", "PostgreSQL") == "entities/prod-cluster__node.1__postgresql.yaml"


def test_discover_cluster_param_threads_into_host_row_and_details():
    d = discover_host("203.0.113.20", "prod", cluster="k3s-prod", runner=_default_runner())
    assert d["host"]["cluster"] == "k3s-prod"
    names = {s["name"]: s for s in d["services"]}
    # 显式 cluster → L3 文件名用 cluster，不再用 env 兜底。
    assert names["harbor"]["detail"] == "entities/k3s-prod__203.0.113.20__harbor.yaml"
    assert names["harbor"]["cluster"] == "k3s-prod"
    assert d["details"]["harbor"]["cluster"] == "k3s-prod"
    assert d["details"]["harbor"]["detail"] == names["harbor"]["detail"]


def test_discover_credential_derived_from_key_path_only():
    runner = _default_runner()
    d = discover_host("203.0.113.20", "prod",
                      {"user": "ops", "key_path": "/keys/node1.pem"}, runner=runner)
    assert d["host"]["credential"] == {
        "type": "ssh_key", "ref": "/keys/node1.pem", "user": "ops", "port": 22,
    }
    # 密码走 askpass（无 key_path）→ 不写凭据引用，密码明文不进任何输出。
    d2 = discover_host("203.0.113.20", "prod",
                       {"user": "root", "password": "Sup3r-Secret!Password"}, runner=runner)
    assert "credential" not in d2["host"]
    assert "Sup3r-Secret!Password" not in json.dumps(d2)


def test_discover_ssh_failure_raises_no_partial_data():
    runner = _default_runner(**{"docker ps": DiscoveryError("SSH 连接 203.0.113.20 失败")})
    with pytest.raises(DiscoveryError) as exc:
        discover_host("203.0.113.20", "prod", runner=runner)
    assert "203.0.113.20" in str(exc.value)


def test_discover_docker_unavailable_falls_through_to_other_probes():
    """docker 未安装（exit 127）→ 记录 skipped，其余探针照常。"""
    runner = _default_runner(**{"docker ps": ("", 127), "compose ls": ("", 127)})
    d = discover_host("203.0.113.20", "prod", runner=runner)
    assert d["probes"]["docker"] != "ok"
    assert d["probes"]["kubectl"] == "ok"
    assert d["probes"]["ss"] == "ok"
    assert {s["name"] for s in d["services"]} == {"grafana", "unidentified-9090"}
    # 无 docker 容器 → runtime 跟随 k8s。
    assert d["host"]["runtime"] == "k3s"


def test_discover_parses_compose_ls_plain_fallback():
    runner = _default_runner(
        **{"compose ls": "NAME    STATUS         CONFIG FILES\nharbor  running(1)    /opt/harbor/x.yaml\n"}
    )
    d = discover_host("203.0.113.20", "prod", runner=runner)
    assert d["host"]["attrs"]["compose_projects"] == ["harbor"]


def test_discover_docker_permission_denied_is_explicit():
    runner = _default_runner(
        **{
            "docker ps": ProbeResult("", 1, "permission denied while trying to connect to the Docker daemon"),
            "compose ls": ProbeResult("", 1, "permission denied"),
        }
    )
    d = discover_host("203.0.113.20", "prod", runner=runner)
    assert "权限不足" in d["probes"]["docker"]
    assert "--sudo-password" in d["probes"]["docker"]


def test_discover_systemctl_services_merged_and_docker_priority():
    runner = _default_runner(**{"systemctl": SYSTEMCTL})
    d = discover_host("203.0.113.20", "prod", runner=runner)
    names = {s["name"]: s for s in d["services"]}
    assert d["probes"]["systemctl"] == "ok"
    assert names["node-exporter"]["type"] == "systemd-service"
    assert names["node-exporter"]["attrs"]["unit"] == "node-exporter.service"
    assert names["node-exporter"]["attrs"]["source_probe"] == "systemctl"
    # systemctl 中出现 harbor 与 docker 容器同名 → docker 优先，不生成 systemd 行。
    assert names["harbor"]["type"] == "service"
    assert "inactive" not in names


def test_discover_systemctl_unavailable_is_skipped():
    d = discover_host("203.0.113.20", "prod", runner=_default_runner())
    assert d["probes"]["systemctl"] == "skipped（systemctl 不可用）"


def test_discover_skip_unidentified_filters_ss_entries():
    d = discover_host("203.0.113.20", "prod", runner=_default_runner(),
                      skip_unidentified=True)
    assert not any(s["name"].startswith("unidentified-") for s in d["services"])
    # 已识别服务照常保留。
    assert "harbor" in {s["name"] for s in d["services"]}


def test_discover_ss_entries_record_source_probe():
    d = discover_host("203.0.113.20", "prod", runner=_default_runner())
    svc = next(s for s in d["services"] if s["name"] == "unidentified-9090")
    assert svc["attrs"]["source_probe"] == "ss"
    assert d["details"]["unidentified-9090"]["attrs"]["source_probe"] == "ss"
    assert d["details"]["unidentified-9090"]["detail"] == svc["detail"]


def test_build_ssh_runner_encrypted_key_uses_askpass_without_plaintext(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    import tools.topo_discovery as topodisc

    key = tmp_path / "id_ed25519"
    passphrase = "test-key-passphrase-42"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-f", str(key)],
        check=True, capture_output=True,
    )
    vault = tmp_path / "vault"
    vault.write_text(passphrase, encoding="utf-8")
    askpass = topodisc._make_askpass_script(vault)
    calls = {}

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "ssh":
            calls["argv"] = argv
            calls["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="connected", stderr="")
        if argv[:2] == ["/bin/sh", str(askpass)]:
            return SimpleNamespace(returncode=0, stdout=passphrase + "\n", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    runner = topodisc._build_ssh_runner(
        "203.0.113.20", "root", key_path=str(key), key_passphrase_file=askpass
    )
    result = runner("echo ok")

    assert result.ok is True
    assert "BatchMode=yes" not in calls["argv"]
    assert "PreferredAuthentications=publickey" in calls["argv"]
    assert calls["kwargs"]["env"]["SSH_ASKPASS"] == str(askpass)
    blob = json.dumps(calls["argv"]) + json.dumps(calls["kwargs"]["env"])
    assert passphrase not in blob


def test_build_ssh_runner_sudo_password_uses_stdin_not_command_string(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import tools.topo_discovery as topodisc

    sudo_secret = "sudo-secret-77"
    vault = tmp_path / "vault"
    vault.write_text(sudo_secret, encoding="utf-8")
    askpass = topodisc._make_askpass_script(vault)
    calls = {}

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "ssh":
            calls["argv"] = argv
            calls["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        if argv[:2] == ["/bin/sh", str(askpass)]:
            return SimpleNamespace(returncode=0, stdout=sudo_secret + "\n", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    runner = topodisc._build_ssh_runner(
        "203.0.113.20", "root", sudo_password_file=askpass
    )
    result = runner("docker ps")

    assert result.ok is True
    assert "sudo -S -p '' docker ps" in calls["argv"][-1]
    assert calls["kwargs"]["input"] == sudo_secret + "\n"
    blob = json.dumps(calls["argv"]) + json.dumps(calls["kwargs"].get("env", {}))
    assert sudo_secret not in blob


def test_cli_short_options_parse_and_flow(tmp_path, monkeypatch):
    import hermes_cli.topo_discover as td

    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(home))
    seen = {}

    def fake_prompt(host, user, key, **kwargs):
        seen.update(kwargs)
        return {"user": user}

    def fake_discover(host, env, creds, **kwargs):
        return _discovery()

    monkeypatch.setattr(td, "_prompt_credentials", fake_prompt)
    monkeypatch.setattr(td, "discover_host", fake_discover)
    monkeypatch.setattr(td, "write_discovery", lambda *a, **kw: {"written": []})

    rc = td.main([
        "-H", "203.0.113.20", "-e", "prod", "-u", "root", "-k", "/tmp/id_ed25519",
        "--dry-run", "--yes",
    ])
    assert rc == 0


def test_cli_password_stdin_flows_to_prompt_credentials(tmp_path, monkeypatch):
    import io

    import hermes_cli.topo_discover as td

    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(home))
    monkeypatch.setattr(td.sys, "stdin", io.StringIO("pipe-secret\n"))
    seen = {}

    def fake_prompt(host, user, key, **kwargs):
        seen.update(kwargs)
        return {"user": user}

    monkeypatch.setattr(td, "_prompt_credentials", fake_prompt)
    monkeypatch.setattr(td, "discover_host", lambda *a, **kw: _discovery())
    monkeypatch.setattr(td, "write_discovery", lambda *a, **kw: {"written": []})

    rc = td.main([
        "--host", "203.0.113.20", "--env", "prod", "--password-stdin",
        "--dry-run", "--yes",
    ])
    assert rc == 0
    assert seen["password"] == "pipe-secret"


def test_host_expansion_ranges_and_lists():
    import hermes_cli.topo_discover as td

    assert td._expand_hosts(["10.123.66.23[3-8]"]) == [
        "10.123.66.233", "10.123.66.234", "10.123.66.235",
        "10.123.66.236", "10.123.66.237", "10.123.66.238",
    ]
    assert td._expand_hosts(["10.123.66.233,10.123.66.234"]) == [
        "10.123.66.233", "10.123.66.234",
    ]


def test_cli_batch_scan_continues_after_single_failure(tmp_path, monkeypatch):
    import hermes_cli.topo_discover as td

    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(home))
    monkeypatch.setattr(td, "_prompt_credentials", lambda host, user, key: {"user": user})
    calls = []

    def fake_discover(host, env, creds, **kwargs):
        calls.append(host)
        if host.endswith(".234"):
            raise DiscoveryError("connection refused")
        return _discovery()

    monkeypatch.setattr(td, "discover_host", fake_discover)
    monkeypatch.setattr(td, "write_discovery", lambda *a, **kw: {"written": []})

    rc = td.main(["--host", "10.123.66.23[3-8]", "--env", "prod", "--dry-run", "--yes"])
    assert rc == 0
    assert len(calls) == 6


def test_cli_batch_confirm_once_and_writes_each_success(tmp_path, monkeypatch):
    import hermes_cli.topo_discover as td

    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(home))
    monkeypatch.setattr(td, "_prompt_credentials", lambda host, user, key: {"user": user})
    monkeypatch.setattr(td, "discover_host", lambda host, env, creds, **kw: _discovery())
    writes = []
    monkeypatch.setattr(td, "write_discovery", lambda *a, **kw: writes.append(a) or {"written": []})
    confirm_calls = []
    monkeypatch.setattr(td, "_confirm", lambda force, yes: confirm_calls.append(1) or True)

    rc = td.main(["--host", "10.123.66.23[3-8]", "--env", "prod", "--yes"])
    assert rc == 0
    assert confirm_calls == [1]
    assert len(writes) == 6


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

def _discovery(host: str = "203.0.113.20", env: str = "prod"):
    return discover_host(host, env, runner=_default_runner())


def test_write_discovery_writes_v3_structure(tmp_path):
    home = tmp_path / "hermes_home"
    home.mkdir()
    d = _discovery()
    result = write_discovery(home, d)
    assert result["written"]

    index = home / "hosts" / "203.0.113.20.yaml"
    assert index.is_file()
    data = yaml.safe_load(index.read_text(encoding="utf-8"))
    assert data["host"] == "203.0.113.20"
    assert data["env"] == "prod"
    assert data["cluster"] == "default"
    assert {s["name"] for s in data["services"]} == {
        "harbor", "postgres", "grafana", "unidentified-9090"}

    topo = yaml.safe_load((home / "topology.yaml").read_text(encoding="utf-8"))
    assert topo["version"] == 3
    assert topo["hosts"][0]["name"] == "203.0.113.20"
    assert topo["hosts"][0]["cluster"] == "default"
    assert topo["hosts"][0]["services_index"] == "hosts/203.0.113.20.yaml"

    # L3 命名：entities/{env}__{host}__{name}.yaml（cluster 空 → env 兜底）。
    harbor = home / "entities" / "prod__203.0.113.20__harbor.yaml"
    assert harbor.is_file()
    assert yaml.safe_load(harbor.read_text(encoding="utf-8"))["name"] == "harbor"

    # 落盘结果能被 topo_tools 正常读取（第二层服务索引进扁平视图）。
    os.environ["VIGIL_HOME"] = str(home)
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        from tools.topo_tools import topo_query
        node = json.loads(topo_query(host="203.0.113.20"))
        assert {s["name"] for s in node["services"]} >= {"harbor", "postgres"}
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


def test_write_discovery_same_app_two_hosts_no_entity_collision(tmp_path):
    """L3 命名验收：同一应用跨两个 host 发现 → 两个实体文件不冲突。"""
    home = tmp_path / "hermes_home"
    home.mkdir()
    write_discovery(home, _discovery("node-a"))
    write_discovery(home, _discovery("node-b"))
    a = home / "entities" / "prod__node-a__harbor.yaml"
    b = home / "entities" / "prod__node-b__harbor.yaml"
    assert a.is_file() and b.is_file()
    assert a != b
    assert yaml.safe_load(a.read_text(encoding="utf-8"))["name"] == "harbor"
    assert yaml.safe_load(b.read_text(encoding="utf-8"))["name"] == "harbor"


def test_write_discovery_legacy_env_maps_to_tier_in_environments(tmp_path):
    """写入端只产四值枚举：老自定义 env（uat）按档位映射为 prod，不追加 uat 定义。"""
    home = tmp_path / "hermes_home"
    home.mkdir()
    d = _discovery("node-a", env="uat")
    write_discovery(home, d)
    topo = yaml.safe_load((home / "topology.yaml").read_text(encoding="utf-8"))
    envs = [e["name"] for e in topo["environments"]]
    assert envs == ["prod"]
    assert topo["environments"][0]["isolation"] == "strict"


def test_write_discovery_refuses_v1_topology(tmp_path):
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "topology.yaml").write_text(
        "version: 1\ncore_entities:\n  - {name: old, type: svc, env: prod}\n",
        encoding="utf-8",
    )
    d = _discovery()
    with pytest.raises(DiscoveryError) as exc:
        write_discovery(home, d)
    assert "v0.1" in str(exc.value)
    # v0.1 数据未被改写。
    assert "version: 1" in (home / "topology.yaml").read_text(encoding="utf-8")


def test_write_discovery_refuses_existing_host_unless_force(tmp_path):
    home = tmp_path / "hermes_home"
    home.mkdir()
    d = _discovery()
    write_discovery(home, d)
    with pytest.raises(DiscoveryError) as exc:
        write_discovery(home, d)  # 已存在 → 拒绝
    assert "--force" in str(exc.value)

    result = write_discovery(home, d, force=True)  # --force → 覆盖
    assert result["written"]


def test_write_discovery_dry_run_not_applied_by_cli(tmp_path, monkeypatch):
    """CLI --dry-run 只展示不落盘（main 走 dry-run 分支不调 write_discovery）。"""
    import hermes_cli.topo_discover as td

    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(home))
    calls = {"discover": 0, "write": 0}

    def fake_discover(*a, **kw):
        calls["discover"] += 1
        return _discovery()

    def fake_write(*a, **kw):
        calls["write"] += 1
        return {"written": []}

    monkeypatch.setattr(td, "discover_host", fake_discover)
    monkeypatch.setattr(td, "write_discovery", fake_write)
    monkeypatch.setattr(td, "_prompt_credentials", lambda host, user, key: {"user": "root"})

    rc = td.main(["--host", "203.0.113.20", "--env", "prod", "--dry-run", "--yes"])
    assert rc == 0
    assert calls["discover"] == 1
    assert calls["write"] == 0
    assert not (home / "topology.yaml").exists()
    assert not (home / "hosts").exists()


def test_topo_discover_cli_help_and_missing_args(tmp_path):
    import subprocess
    import sys
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "topo-discover", "--help"],
        cwd=Path(__file__).resolve().parents[2],
        env=dict(os.environ),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0
    assert "--host" in proc.stdout and "--env" in proc.stdout
    assert "--dry-run" in proc.stdout and "--force" in proc.stdout
