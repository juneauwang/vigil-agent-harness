"""YAPL P1（批次五十二）：拓扑 schema v0.4 数据层验收测试。

覆盖：
  - 词表层 schemas.yaml：内置默认枚举、validate_enum / validate_enum_list
    （unknown 兜底，受控词表不让自由文本溜入）、export_schemas_yaml 落地；
  - 发现引擎 v0.4：type 判定表归类、runtime/role 数组、credentials 数组、
    managed_by 枚举、L3 档案 snapshot 二维分支（by_type/by_runtime）、
    硬件层（cpu/mem/disk/gpu/raid/network/firewall controller 枚举）；
  - 落盘 v0.4：services/<host>.yaml 路径、entities/{cluster}__{host}__{name}.yaml
    命名 + detail 路径键、hardware/<host>.yaml、topology.yaml version: 4
    （无 sources/cross_host/key_paths/services_index）；
  - 读取端：services/ 优先、hosts/ 兼容回退；topo_query 无参总览不含
    cross_host/key_paths；服务行 depends_on 上移第二层。
"""

from __future__ import annotations

import json

import pytest
import yaml

from tools.topo_discovery import (
    ProbeResult,
    _classify_service_type,
    _host_roles_for,
    _parse_df_disks,
    _parse_ip_addr,
    _parse_lscpu,
    _parse_meminfo_gb,
    discover_host,
    write_discovery,
)
from tools.topo_schemas import (
    default_schemas,
    export_schemas_yaml,
    load_schemas,
    managed_by_command,
    schema_list,
    validate_enum,
    validate_enum_list,
)

# ---------------------------------------------------------------------------
# 词表层 schemas.yaml（9.6/9.7）
# ---------------------------------------------------------------------------


def test_default_schemas_cover_9_7_enum_tables():
    schemas = default_schemas()
    assert schemas["entity_types"] == [
        "db", "cache", "queue", "registry", "monitor", "gateway", "search",
        "object_storage", "app", "unknown",
    ]
    assert schemas["cluster_types"] == ["k8s", "k3s", "kind", "docker", "bare"]
    assert "control-plane" in schemas["host_roles"]
    assert schemas["host_runtimes"] == ["bare", "docker", "containerd", "k3s", "k8s", "podman"]
    assert "docker" in schemas["managed_by"] and "unknown" in schemas["managed_by"]
    assert schemas["gpu_controller"] == ["nvidia-smi", "npu-smi", "cambricon-smi", "rocm-smi", "none"]
    assert schemas["firewall_controller"] == ["firewalld", "iptables", "ufw", "nftables", "none"]
    assert "mdadm" in schemas["raid_tool"]
    assert "standalone" in schemas["db_role"]
    assert "scrape" in schemas["monitor_collection_mode"]


def test_validate_enum_fallback_unknown(tmp_path):
    # 受控枚举：合法值原样；自由文本/空 → unknown 兜底（LLM/探针自由文本不进表）。
    assert validate_enum("db", "entity_types") == "db"
    assert validate_enum("sql-server-2019", "entity_types") == "unknown"
    assert validate_enum("", "entity_types") == "unknown"
    assert validate_enum("-", "entity_types") == "unknown"
    assert validate_enum("k3s", "host_runtimes") == "k3s"
    assert validate_enum("podman", "host_runtimes") == "podman"
    assert validate_enum("", "host_runtimes") == "unknown"


def test_validate_enum_list_filters_and_fallback():
    assert validate_enum_list(["docker", "podman", "weird"], "host_runtimes") == ["docker", "podman"]
    assert validate_enum_list(["nope"], "host_roles", fallback="standalone") == ["standalone"]
    assert validate_enum_list([], "host_roles") == []


def test_managed_by_command_mapping():
    assert managed_by_command("docker") == "docker"
    assert managed_by_command("docker_compose") == "docker compose"
    assert managed_by_command("kubectl") == "kubectl"
    assert managed_by_command("systemd") == "systemctl"
    assert managed_by_command("bare") is None
    assert managed_by_command("unknown") is None


def test_export_and_load_schemas_yaml(tmp_path):
    home = tmp_path / "hermes_home"
    home.mkdir()
    path = export_schemas_yaml(home)
    assert path is not None and path.is_file()
    # 再导出不覆盖（用户自定义演进不被冲掉）。
    assert export_schemas_yaml(home) is None
    loaded = load_schemas(home)
    assert loaded["entity_types"] == default_schemas()["entity_types"]
    # 文件缺失 → 内置默认兜底。
    assert load_schemas(tmp_path / "missing")["entity_types"] == default_schemas()["entity_types"]


# ---------------------------------------------------------------------------
# type 判定表 + runtime/role 启发（9.7）
# ---------------------------------------------------------------------------


def test_classify_service_type_9_7_table():
    assert _classify_service_type("postgres") == "db"
    assert _classify_service_type("mysql-master") == "db"
    assert _classify_service_type("redis-cache") == "cache"
    assert _classify_service_type("rabbitmq") == "queue"
    assert _classify_service_type("harbor", "harbor:v2.11.0") == "registry"
    assert _classify_service_type("prometheus") == "monitor"
    assert _classify_service_type("nginx-proxy") == "gateway"
    assert _classify_service_type("minio") == "object_storage"
    assert _classify_service_type("meilisearch") == "search"
    # 命中不了诚实给 app（不猜 db/cache）。
    assert _classify_service_type("order-api") == "app"
    assert _classify_service_type("cron-worker") == "app"


def test_host_roles_for_runtime_heuristic():
    assert _host_roles_for("docker") == ["docker-host"]
    assert _host_roles_for("k3s") == ["worker"]
    assert _host_roles_for("k8s") == ["worker"]
    assert _host_roles_for("") == []


# ---------------------------------------------------------------------------
# 硬件层静态规格解析
# ---------------------------------------------------------------------------

LSCPU = """\
Architecture:            x86_64
CPU(s):                  16
Model name:              AMD EPYC 7K62 48-Core Processor
Thread(s) per core:      2
"""

MEMINFO = """\
MemTotal:       33554432 kB
MemFree:        11264000 kB
"""

DF = """\
Filesystem     1B-blocks FSType Source
/              511355789312 ext4 /dev/sda1
/var/lib/docker 511355789312 ext4 /dev/nvme0n1p1
/dev            511355789312 devtmpfs /dev
/run            511355789312 tmpfs /run
"""

IP_ADDR = """\
1: lo    inet 127.0.0.1/8 scope host lo
2: eth0  inet 10.0.0.5/24 brd 10.0.0.255 scope global eth0
3: eth0  inet6 fe80::cafe/64 scope link
4: eth1  inet 192.168.1.5/24 brd 192.168.1.255 scope global eth1
"""


def test_parse_lscpu_and_meminfo():
    cpu = _parse_lscpu(LSCPU)
    assert cpu == {"model": "AMD EPYC 7K62 48-Core Processor", "cores": 16}
    assert _parse_meminfo_gb(MEMINFO) == 32


def test_parse_df_disks_filters_pseudo_fs_and_heuristics():
    disks = _parse_df_disks(DF)
    names = {d["mount"]: d for d in disks}
    assert set(names) == {"/", "/var/lib/docker"}
    assert names["/"]["type"] == "hdd"      # /dev/sd* → hdd
    assert names["/var/lib/docker"]["type"] == "ssd"  # nvme → ssd
    assert names["/"]["size_gb"] == 511


def test_parse_ip_addr_skips_loopback_link_local():
    ifaces = _parse_ip_addr(IP_ADDR)
    assert [i["name"] for i in ifaces] == ["eth0", "eth1"]
    assert ifaces[0]["primary"] is True and ifaces[1]["primary"] is False
    assert all(i["ip"] not in ("127.0.0.1",) and not i["ip"].startswith("fe80:") for i in ifaces)


def test_discover_hardware_layer_controller_enums():
    """硬件探针产 controller 枚举（不落动态负载/规则内容）。"""
    from tests.tools.test_topo_discovery import FakeRunner

    runner = FakeRunner(**{
        "lscpu": LSCPU,
        "cat /proc/meminfo": MEMINFO,
        "df -B1": DF,
        "command -v ssacli": "mdadm",
        "mdadm --detail --scan": "ARRAY /dev/md0 level=raid1",
        "lspci": "02:00.0 RAID bus controller: LSI MegaRAID",
        "command -v nvidia-smi": "/usr/bin/nvidia-smi\n",
        "nvidia-smi --query-gpu=name": "NVIDIA A100-SXM4-40GB\n",
        "ip -o addr": IP_ADDR,
        "systemctl is-active firewalld": "inactive\n",
        "ufw status": "Status: active\n",
    })
    d = discover_host("10.0.0.5", "prod", runner=runner)
    hw = d["hardware"]
    assert hw["hardware"]["cpu"]["cores"] == 16
    assert hw["hardware"]["memory_gb"] == 32
    assert hw["hardware"]["storage"]["raid_tool"] == "mdadm"
    assert hw["hardware"]["storage"]["raid_level"] == "raid1"
    assert hw["hardware"]["storage"]["disks"]
    assert hw["hardware"]["gpu"]["controller"] == "nvidia-smi"
    assert hw["hardware"]["gpu"]["devices"] == [{"model": "NVIDIA A100-SXM4-40GB", "count": 1}]
    assert hw["network"]["firewall"]["controller"] == "ufw"
    assert [i["ip"] for i in hw["network"]["interfaces"]] == ["10.0.0.5", "192.168.1.5"]
    # 动态内容不落盘：无负载/健康数值字段。
    assert "load" not in json.dumps(hw)


# ---------------------------------------------------------------------------
# 落盘 v0.4 结构
# ---------------------------------------------------------------------------

from tests.tools.test_topo_discovery import _discovery  # noqa: E402


def test_write_discovery_v4_layout(tmp_path):
    home = tmp_path / "hermes_home"
    home.mkdir()
    d = _discovery("node-a", env="dev")
    write_discovery(home, d)

    # 第二层目录 services/（hosts/ 不再产出）。
    index = home / "services" / "node-a.yaml"
    assert index.is_file()
    assert not (home / "hosts").exists()
    data = yaml.safe_load(index.read_text(encoding="utf-8"))
    assert data["host"] == "node-a"
    svc = next(s for s in data["services"] if s["name"] == "harbor")
    assert svc["managed_by"] == "docker_compose"
    assert svc["extra_ports"] == [] and svc["log_paths"] == [] and svc["depends_on"] == []
    assert "env" not in svc and "cluster" not in svc   # 继承主机，不冗余

    # 第三层命名 + detail 路径键 + snapshot 二维分支。
    ent = home / "entities" / "dev__node-a__harbor.yaml"
    assert ent.is_file()
    entity = yaml.safe_load(ent.read_text(encoding="utf-8"))
    assert entity["detail"] == "entities/dev__node-a__harbor.yaml"
    assert entity["snapshot"]["source"] == "discovered"
    assert entity["snapshot"]["by_type"] == {"replication_targets": [], "storage_backend": ""}
    assert entity["snapshot"]["by_runtime"]["docker_compose"]["project"] == "harbor"
    assert "type" not in entity and "env" not in entity and "cluster" not in entity

    # 硬件层 hardware/<host>.yaml。
    hw = home / "hardware" / "node-a.yaml"
    assert hw.is_file()
    assert yaml.safe_load(hw.read_text(encoding="utf-8"))["hardware"]["gpu"]["controller"] == "nvidia-smi"

    # 第一层 version 4，旧顶层字段删除。
    topo = yaml.safe_load((home / "topology.yaml").read_text(encoding="utf-8"))
    assert topo["version"] == 4
    for gone in ("sources", "services_index", "cross_host", "key_paths"):
        assert gone not in topo
    host_row = topo["hosts"][0]
    assert host_row["role"] == ["docker-host"]
    assert host_row["runtime"] == ["docker"]
    assert "services_index" not in host_row


def test_snapshot_by_type_branches_per_service():
    """db/cache/registry/monitor 的 by_type 分支各就其位。"""
    from tests.tools.test_topo_discovery import FakeRunner, DOCKER_PS, SS_TLNP

    runner = FakeRunner(**{
        "docker ps": DOCKER_PS,
        "compose ls": "",
        "kubectl": "",
        "ss -tlnp": SS_TLNP,
        "nvidia-smi": "/usr/bin/nvidia-smi\n",
    })
    d = discover_host("node-x", "dev", runner=runner)
    by_name = {n: det["snapshot"]["by_type"] for n, det in d["details"].items()}
    assert by_name["db"] == {"backup_dir": "", "role": "standalone"}
    assert by_name["harbor"] == {"replication_targets": [], "storage_backend": ""}


# ---------------------------------------------------------------------------
# 读取端 v0.4
# ---------------------------------------------------------------------------


def test_load_host_index_services_preferred_hosts_fallback(tmp_path, monkeypatch):
    """services/ 优先；老 hosts/ 兼容回退。"""
    import hermes_cli.config as hc
    from tools.topo_tools import _load_host_index, load_topology

    home = tmp_path / "vigil_home"
    (home / "services").mkdir(parents=True)
    (home / "hosts").mkdir(parents=True)
    (home / "services" / "h1.yaml").write_text(
        "host: h1\nupdated_at: '2026-08-23'\nservices:\n  - {name: new-svc, type: app}\n",
        encoding="utf-8",
    )
    (home / "hosts" / "h1.yaml").write_text(
        "host: h1\nservices:\n  - {name: old-svc, type: service}\n",
        encoding="utf-8",
    )
    host_row = {"name": "h1", "env": "dev"}
    index = _load_host_index(home, host_row)
    assert {s["name"] for s in index["services"]} == {"new-svc"}

    # 只有 hosts/ 的老数据 → 回退命中。
    (home / "services" / "h1.yaml").unlink()
    index2 = _load_host_index(home, host_row)
    assert {s["name"] for s in index2["services"]} == {"old-svc"}


def test_topo_query_overview_v4_no_old_sections(tmp_path, monkeypatch):
    """无参总览：v0.4 不返回 cross_host/key_paths；host 行 v0.4 形态。"""
    import hermes_cli.config as hc
    from tools.topo_tools import topo_query

    home = tmp_path / "vigil_home"
    home.mkdir()
    (home / "topology.yaml").write_text(
        "version: 4\nupdated_at: '2026-08-23'\n"
        "environments:\n  - {name: dev, isolation: relaxed, role: dev}\n"
        "clusters:\n  - {name: k3s-dev, env: dev, type: k3s, provenance: manual, source: manual}\n"
        "hosts:\n"
        "  - {name: node1, type: host, env: dev, cluster: k3s-dev, endpoint: '10.0.0.5', "
        "role: [worker], runtime: [k3s], source: manual, last_verified: '2026-08-23'}\n",
        encoding="utf-8",
    )
    (home / "services").mkdir()
    (home / "services" / "node1.yaml").write_text(
        "host: node1\nservices:\n"
        "  - {name: nginx, type: gateway, managed_by: helm, endpoint: '10.0.0.5:80', "
        "depends_on: [postgres]}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        overview = json.loads(topo_query(home=home))
    finally:
        hc._LOAD_CONFIG_CACHE.clear()
    assert overview["version"] == 4
    assert "cross_host" not in overview and "key_paths" not in overview
    assert overview["clusters"][0]["type"] == "k3s"
    # 无参总览 = 紧凑行（OPS-DELTA #30 方案 1：name/type/env/cluster/endpoint/stale）。
    host = overview["hosts"][0]
    assert host["name"] == "node1" and host["endpoint"] == "10.0.0.5"
    # host= 展开返回完整第一层 host 行（role/runtime 数组）。
    node = json.loads(topo_query(host="node1", home=home))
    assert node["role"] == ["worker"] and node["runtime"] == ["k3s"]
    assert node["type"] == "host"
    nginx = next(s for s in node["services"] if s["name"] == "nginx")
    assert nginx["depends_on"] == ["postgres"]


def test_build_view_service_cluster_inherits_host(tmp_path):
    """P1 遗留待办 #2：API（build_view）服务行 cluster 从所属 host 继承。

    v0.4 服务行不冗余 cluster（设计 §9.3）；读取端应继承 host.cluster，
    而非兜底 default（API 曾显示服务挂 default 集群）。
    """
    from hermes_cli.subcommands.topo_export import build_view

    home = tmp_path / "vigil_home"
    (home / "services").mkdir(parents=True)
    (home / "topology.yaml").write_text(
        "version: 4\n"
        "environments:\n  - {name: prod, isolation: strict, role: prod}\n"
        "clusters:\n  - {name: k3s-prod, env: prod, type: k3s}\n"
        "hosts:\n"
        "  - {name: node1, type: host, env: prod, cluster: k3s-prod, endpoint: '10.0.0.1', "
        "role: [worker], runtime: [k3s], source: manual, last_verified: '2026-08-23'}\n",
        encoding="utf-8",
    )
    (home / "services" / "node1.yaml").write_text(
        "host: node1\nservices:\n"
        "  - {name: harbor, type: registry, managed_by: docker}\n"
        "  - {name: nginx, type: gateway, managed_by: helm, cluster: k3s-prod}\n",
        encoding="utf-8",
    )
    view = build_view(home)
    assert view is not None
    host = view["hosts"][0]
    assert host["card"]["cluster"] == "k3s-prod"
    svc_cards = {s["card"]["name"]: s["card"]["cluster"] for s in host["services"]}
    # 服务行无 cluster → 继承 host；显式 cluster 仍优先。
    assert svc_cards["harbor"] == "k3s-prod"
    assert svc_cards["nginx"] == "k3s-prod"
