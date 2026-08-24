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
import socket
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

SS_TLNP_SYSTEMD = """\
State Recv-Q Send-Q Local Address:Port Peer Address:Port Process
LISTEN 0      128    0.0.0.0:22      0.0.0.0:*    users:(("sshd",pid=1,fd=3))
LISTEN 0      128    0.0.0.0:9100    0.0.0.0:*    users:(("node_exporter",pid=2,fd=4))
"""

SS_TLNP_FULL = """\
State Recv-Q Send-Q Local Address:Port Peer Address:Port Process
LISTEN 0      128    0.0.0.0:22      0.0.0.0:*    users:(("sshd",pid=1,fd=3))
LISTEN 0      128    0.0.0.0:9100    0.0.0.0:*    users:(("node_exporter",pid=2,fd=4))
LISTEN 0      128    0.0.0.0:9090    0.0.0.0:*    users:(("prometheus",pid=3,fd=5))
LISTEN 0      128    0.0.0.0:5432    0.0.0.0:*    users:(("postgres",pid=4,fd=6))
LISTEN 0      128    0.0.0.0:80      0.0.0.0:*    users:(("nginx",pid=5,fd=7))
LISTEN 0      128    0.0.0.0:6379    0.0.0.0:*    users:(("redis",pid=6,fd=8))
LISTEN 0      128    0.0.0.0:3306    0.0.0.0:*    users:(("mysql",pid=7,fd=9))
"""


class FakeRunner:
    """可编程 mock runner：按命令前缀返回输出；记录调用（供明文断言）。"""

    def __init__(self, **probes):
        self.probes = probes
        self.calls: list = []

    def __call__(self, cmd: str) -> ProbeResult:
        self.calls.append(cmd)
        # 最长前缀优先：override 用更具体的 key（如 "kubectl version"）覆盖
        # 泛化 key（"kubectl"），batch74 起 kubectl 有两条探测命令。
        matches = [(k, v) for k, v in self.probes.items() if k in cmd]
        if matches:
            _, value = max(matches, key=lambda kv: len(kv[0]))
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
        "nvidia-smi --query-gpu=name": NVIDIA,
        "nvidia-smi": "/usr/bin/nvidia-smi\n",
    }
    probes.update(overrides)
    return FakeRunner(**probes)


# ---------------------------------------------------------------------------
# 发现引擎
# ---------------------------------------------------------------------------

def test_discover_maps_services_ports_images():
    runner = _default_runner()
    d = discover_host("203.0.113.20", "prod", runner=runner)

    assert d["version"] == 4
    assert d["source"] == "discovered"
    assert d["needs_review"] is True
    assert d["last_verified"]

    host = d["host"]
    assert host["name"] == "203.0.113.20"
    assert host["type"] == "host"
    assert host["env"] == "prod"
    assert host["cluster"] == "default"          # 未标 cluster → 显示 default
    assert host["endpoint"] == "203.0.113.20"
    assert host["runtime"] == ["docker"]         # v0.4：runtime 数组
    assert host["role"] == ["docker-host"]       # v0.4：role 数组
    assert host["source"] == "discovered"
    assert host["needs_review"] is True

    names = {s["name"]: s for s in d["services"]}
    assert "harbor" in names
    # v0.4 服务行：type 判定表归类（registry）+ managed_by + L3 detail 引用。
    assert names["harbor"]["type"] == "registry"
    assert names["harbor"]["managed_by"] == "docker_compose"
    assert names["harbor"]["endpoint"] == "203.0.113.20:30443"
    assert names["harbor"]["detail"] == "entities/prod__203.0.113.20__harbor.yaml"
    # compose 项目 db（service postgres，无同名 service）→ 聚合为项目名 db 一条。
    assert names["db"]["type"] == "db"
    assert names["db"]["managed_by"] == "docker_compose"
    assert names["db"]["endpoint"] == "203.0.113.20:5432"
    # k8s 枚举：grafana svc（nodePort 30030）+ deploy（镜像）→ managed_by kubectl。
    assert names["grafana"]["type"] == "monitor"
    assert names["grafana"]["managed_by"] == "kubectl"
    assert names["grafana"]["endpoint"] == "203.0.113.20:30030"
    # 端口扫描补条目：9090 未识别 → pending_review；22（sshd）与已映射端口不补。
    assert "unidentified-9090" not in names
    assert {s["name"] for s in d["pending_review"]} == {"unidentified-9090"}
    assert "unidentified-22" not in names
    assert "unidentified-5432" not in names

    # 第三层详情草案：L3 命名 entities/{cluster}__{host}__{name}.yaml，
    # cluster 空用 env 兜底（本用例无 cluster → prod__host__name）；档案带
    # detail 路径键 + snapshot 二维分支（common/by_type/by_runtime）。
    assert d["details"]["harbor"]["name"] == "harbor"
    assert d["details"]["harbor"]["detail"] == names["harbor"]["detail"]
    assert d["details"]["harbor"]["snapshot"]["common"]["version"] == "v2.11.0"
    assert d["details"]["harbor"]["snapshot"]["by_type"] == {
        "replication_targets": [], "storage_backend": "",
    }
    assert d["details"]["harbor"]["snapshot"]["by_runtime"]["docker_compose"]["project"] == "harbor"
    assert d["details"]["harbor"]["snapshot"]["by_runtime"]["docker_compose"]["services"] == [
        {"name": "harbor", "state": "running"},
    ]
    assert d["details"]["db"]["snapshot"]["by_type"] == {
        "backup_dir": "", "role": "standalone",
    }
    assert names["grafana"]["detail"] == "entities/prod__203.0.113.20__grafana.yaml"

    # 硬件层：GPU 探针出 controller 枚举 + devices（静态规格，不落动态数据）。
    gpu = d["hardware"]["hardware"]["gpu"]
    assert gpu["controller"] == "nvidia-smi"
    assert gpu["devices"] == [{"model": "NVIDIA A100-SXM4-40GB, 40960 MiB", "count": 1}]


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
    # 显式 cluster → L3 文件名用 cluster，不再用 env 兜底；档案 detail 同款。
    assert names["harbor"]["detail"] == "entities/k3s-prod__203.0.113.20__harbor.yaml"
    assert d["details"]["harbor"]["detail"] == names["harbor"]["detail"]
    assert "cluster" not in d["details"]["harbor"]   # v0.4：L3 不放 type/env/cluster


def test_discover_credential_derived_from_key_path_only():
    runner = _default_runner()
    d = discover_host("203.0.113.20", "prod",
                      {"user": "ops", "key_path": "/keys/node1.pem"}, runner=runner)
    # v0.4：credential 单对象 → credentials 数组（ssh_key/vault 多凭据）。
    assert d["host"]["credentials"] == [
        {"type": "ssh_key", "ref": "/keys/node1.pem", "user": "ops", "port": 22},
    ]
    assert "credential" not in d["host"]              # v0.4：只有 credentials 数组
    # 密码走 askpass（无 key_path）→ 不写凭据引用，密码明文不进任何输出。
    d2 = discover_host("203.0.113.20", "prod",
                       {"user": "root", "password": "Sup3r-Secret!Password"}, runner=runner)
    assert "credentials" not in d2["host"]
    assert "Sup3r-Secret!Password" not in json.dumps(d2)


def test_discover_ssh_failure_raises_no_partial_data():
    runner = _default_runner(**{"docker ps": DiscoveryError("SSH 连接 203.0.113.20 失败")})
    with pytest.raises(DiscoveryError) as exc:
        discover_host("203.0.113.20", "prod", runner=runner)
    assert "203.0.113.20" in str(exc.value)


def test_discover_docker_unavailable_falls_through_to_other_probes():
    """docker 未安装（exit 127）→ 记录 skipped，其余探针照常。"""
    runner = _default_runner(**{
        "docker ps": ("", 127), "compose ls": ("", 127),
        # 标准 kubeadm：kubectl version 输出无 k3s 特征 → 标准 kubernetes。
        "kubectl version": "",
    })
    d = discover_host("203.0.113.20", "prod", runner=runner)
    assert d["probes"]["docker"] != "ok"
    assert d["probes"]["kubectl"] == "ok"
    assert d["probes"]["ss"] == "ok"
    # v0.4：unidentified 端口进 pending_review（不入 services）。
    assert {s["name"] for s in d["services"]} == {"grafana"}
    assert {s["name"] for s in d["pending_review"]} == {"unidentified-9090"}
    # 无 docker 容器 → runtime 跟随 k8s（v0.4 数组形态）。batch74：默认 runner
    # 的 kubectl 探测输出无 k3s 特征 → 标准 kubernetes，不再一律误标 k3s。
    assert d["host"]["runtime"] == ["kubernetes"]
    assert d["host"]["role"] == ["worker"]


def test_discover_parses_compose_ls_plain_fallback():
    runner = _default_runner(
        **{"compose ls": "NAME    STATUS         CONFIG FILES\nharbor  running(1)    /opt/harbor/x.yaml\n"}
    )
    d = discover_host("203.0.113.20", "prod", runner=runner)
    # v0.4：compose 项目信息不落 host.attrs——probes 记统计，L3 档案
    # snapshot.by_runtime.docker_compose 带 project 名。
    assert d["probes"]["compose"] == "ok(1 projects)"
    assert d["details"]["harbor"]["snapshot"]["by_runtime"]["docker_compose"]["project"] == "harbor"
    assert "attrs" not in d["host"]


# ---------------------------------------------------------------------------
# 批六十七：compose 项目聚合（9.3）+ helm release 探测（9.5 补缺口）
# ---------------------------------------------------------------------------

COMPOSE_PS_MULTI = """\
{"Command":"","ID":"1","Image":"nginx:1.25","Labels":"com.docker.compose.project=web-app,com.docker.compose.service=nginx,com.docker.compose.project.working_dir=/opt/web-app","Names":"web-app-nginx-1","Ports":"0.0.0.0:8080->80/tcp","State":"running"}
{"Command":"","ID":"2","Image":"postgres:16","Labels":"com.docker.compose.project=web-app,com.docker.compose.service=db,com.docker.compose.project.working_dir=/opt/web-app","Names":"web-app-db-1","Ports":"0.0.0.0:5432->5432/tcp","State":"running"}
{"Command":"","ID":"3","Image":"redis:7","Labels":"com.docker.compose.project=web-app,com.docker.compose.service=cache,com.docker.compose.project.working_dir=/opt/web-app","Names":"web-app-cache-1","Ports":"127.0.0.1:6379->6379/tcp, 0.0.0.0:8081->8081/tcp","State":"running"}
"""

COMPOSE_PS_SAME_NAME = """\
{"Command":"","ID":"4","Image":"goharbor/harbor-core:v2.15.1","Labels":"com.docker.compose.project=harbor-local,com.docker.compose.service=core","Names":"harbor-core","Ports":"","State":"running"}
{"Command":"","ID":"5","Image":"goharbor/nginx-photon:v2.15.1","Labels":"com.docker.compose.project=harbor-local,com.docker.compose.service=harbor","Names":"harbor-proxy","Ports":"0.0.0.0:30443->8443/tcp","State":"running"}
"""

COMPOSE_PS_NO_PORT = """\
{"Command":"","ID":"6","Image":"langgenius/dify-api:1.14","Labels":"com.docker.compose.project=dify,com.docker.compose.service=api","Names":"dify-api-1","Ports":"","State":"running"}
{"Command":"","ID":"7","Image":"nginx:latest","Labels":"com.docker.compose.project=dify,com.docker.compose.service=nginx","Names":"dify-nginx-1","Ports":"","State":"restarting"}
"""

DOCKER_RUN_SINGLE = """\
{"Command":"","ID":"8","Image":"registry:2","Labels":"","Names":"kind-registry","Ports":"0.0.0.0:5000->5000/tcp","State":"running"}
"""

KUBE_HARBOR = (
    '{"items":['
    '{"kind":"Service","metadata":{"name":"harbor","namespace":"harbor"},'
    '"spec":{"ports":[{"port":80,"nodePort":30443}]}},'
    '{"kind":"Deployment","metadata":{"name":"harbor","namespace":"harbor"},'
    '"spec":{"template":{"spec":{"containers":[{"image":"goharbor/harbor-core:v2.15.1"}]}}}}'
    ']}'
)

HELM_LIST = json.dumps([
    {"name": "grafana", "namespace": "monitoring", "revision": "3",
     "updated": "2026-08-01", "status": "deployed", "chart": "grafana-8.0.0",
     "app_version": "11.0"},
    {"name": "cilium", "namespace": "kube-system", "revision": "2",
     "updated": "2026-08-01", "status": "deployed", "chart": "cilium-1.16.19",
     "app_version": "1.16.19"},
])


def _docker_runner(**overrides):
    return FakeRunner(**{
        "docker ps": "",
        "compose ls": "",
        "kubectl": "",
        "ss -tlnp": "",
        "nvidia-smi": "",
        **overrides,
    })


def test_compose_project_aggregates_to_single_row():
    """9.3：多容器 compose 项目 → 一条服务行；容器细节进 L3 状态列表。"""
    d = discover_host("10.0.0.9", "dev", runner=_docker_runner(
        **{"docker ps": COMPOSE_PS_MULTI}))
    names = {s["name"]: s for s in d["services"]}
    assert set(names) == {"web-app"}                       # 3 容器 → 1 条
    svc = names["web-app"]
    assert svc["managed_by"] == "docker_compose"
    assert svc["type"] == "gateway"                        # 主 service nginx
    assert svc["endpoint"] == "10.0.0.9:8080"              # 首个 published 端口
    assert svc["extra_ports"] == [5432, 6379, 8081]        # 其余去重
    assert svc["detail"] == "entities/dev__10.0.0.9__web-app.yaml"
    dc = d["details"]["web-app"]["snapshot"]["by_runtime"]["docker_compose"]
    assert dc["project"] == "web-app"
    assert dc["workdir"] == "/opt/web-app"                 # working_dir label
    assert dc["services"] == [                              # 容器状态列表
        {"name": "web-app-nginx-1", "state": "running"},
        {"name": "web-app-db-1", "state": "running"},
        {"name": "web-app-cache-1", "state": "running"},
    ]
    # 项目 published 端口全部进 seen_ports → ss 不补 unidentified。
    assert not any(s["name"].startswith("unidentified-") for s in d["services"])


def test_compose_same_name_service_preferred():
    """命名规则：与项目同名的 compose service 优先（harbor-local → harbor）。"""
    d = discover_host("10.0.0.9", "dev", runner=_docker_runner(
        **{"docker ps": COMPOSE_PS_SAME_NAME}))
    names = {s["name"]: s for s in d["services"]}
    assert set(names) == {"harbor"}
    assert names["harbor"]["type"] == "registry"
    assert names["harbor"]["managed_by"] == "docker_compose"
    assert names["harbor"]["endpoint"] == "10.0.0.9:30443"
    assert d["details"]["harbor"]["snapshot"]["by_runtime"]["docker_compose"][
        "project"] == "harbor-local"


def test_compose_no_same_name_service_falls_back_project_name():
    """命名规则：无同名 service → 用项目名（dify 项目 → dify）。"""
    d = discover_host("10.0.0.9", "dev", runner=_docker_runner(
        **{"docker ps": COMPOSE_PS_NO_PORT}))
    names = {s["name"]: s for s in d["services"]}
    assert set(names) == {"dify"}
    assert names["dify"]["endpoint"] is None               # 无 published 端口 → null
    assert names["dify"]["type"] == "app"
    assert d["details"]["dify"]["snapshot"]["by_runtime"]["docker_compose"]["workdir"] == ""


COMPOSE_PS_RESERVED = """\
{"Command":"","ID":"r1","Image":"nginx:1.25","Labels":"com.docker.compose.project=docker,com.docker.compose.service=nginx","Names":"docker-nginx-1","Ports":"","State":"running"}
{"Command":"","ID":"r2","Image":"registry:2","Labels":"com.docker.compose.project=docker,com.docker.compose.service=registry","Names":"docker-registry-1","Ports":"0.0.0.0:5000->5000/tcp","State":"running"}
"""

COMPOSE_PS_RESERVED_GENERIC = """\
{"Command":"","ID":"g1","Image":"langgenius/dify-api:1.14","Labels":"com.docker.compose.project=compose,com.docker.compose.service=api","Names":"compose-api-1","Ports":"0.0.0.0:8080->8080/tcp","State":"running"}
{"Command":"","ID":"g2","Image":"nginx:latest","Labels":"com.docker.compose.project=compose,com.docker.compose.service=web","Names":"compose-web-1","Ports":"","State":"running"}
"""


def test_compose_reserved_project_name_uses_business_service():
    """冲突条款：项目名 ∈ 平台保留词（docker）→ 强制用业务主 service 名。"""
    d = discover_host("10.0.0.9", "dev", runner=_docker_runner(
        **{"docker ps": COMPOSE_PS_RESERVED}))
    names = {s["name"]: s for s in d["services"]}
    assert set(names) == {"registry"}                        # 不是 docker
    assert names["registry"]["type"] == "registry"
    assert names["registry"]["managed_by"] == "docker_compose"
    assert names["registry"]["endpoint"] == "10.0.0.9:5000"
    assert d["details"]["registry"]["snapshot"]["by_runtime"]["docker_compose"][
        "project"] == "docker"                                # 事实值不动


def test_compose_reserved_project_generic_main_keeps_project_name():
    """冲突条款兜底：主 service 名也无业务语义（api/web）→ 保留项目名。"""
    d = discover_host("10.0.0.9", "dev", runner=_docker_runner(
        **{"docker ps": COMPOSE_PS_RESERVED_GENERIC}))
    names = {s["name"]: s for s in d["services"]}
    assert set(names) == {"compose"}


def test_compose_non_reserved_project_name_rule_unchanged():
    """回归：非保留词项目名 → 原命名规则不变（项目名兜底）。"""
    d = discover_host("10.0.0.9", "dev", runner=_docker_runner(
        **{"docker ps": COMPOSE_PS_NO_PORT}))
    names = {s["name"]: s for s in d["services"]}
    assert set(names) == {"dify"}                             # 非保留词 → 项目名


def test_compose_name_yields_to_k8s_same_name():
    """命名规则：compose 同名 service 与 k8s 服务重名 → compose 退让项目名。"""
    d = discover_host("10.0.0.9", "dev", runner=_docker_runner(
        **{"docker ps": COMPOSE_PS_SAME_NAME, "kubectl": KUBE_HARBOR}))
    names = {s["name"]: s for s in d["services"]}
    assert names["harbor"]["managed_by"] == "kubectl"      # k8s 行保持
    assert names["harbor"]["endpoint"] == "10.0.0.9:30443"
    assert names["harbor-local"]["managed_by"] == "docker_compose"
    assert d["details"]["harbor"]["snapshot"]["by_runtime"]["kubectl"]["namespace"] == "harbor"
    assert d["details"]["harbor-local"]["snapshot"]["by_runtime"]["docker_compose"][
        "project"] == "harbor-local"


def test_docker_run_single_container_keeps_container_granularity():
    """无 compose_project 的 docker run 单容器 → 容器粒度一条（managed_by docker）。"""
    d = discover_host("10.0.0.9", "dev", runner=_docker_runner(
        **{"docker ps": DOCKER_RUN_SINGLE}))
    names = {s["name"]: s for s in d["services"]}
    assert set(names) == {"kind-registry"}
    svc = names["kind-registry"]
    assert svc["managed_by"] == "docker"
    assert svc["type"] == "registry"
    assert svc["endpoint"] == "10.0.0.9:5000"
    docker_block = d["details"]["kind-registry"]["snapshot"]["by_runtime"]["docker"]
    assert docker_block["containers"] == [{"name": "kind-registry", "state": "running"}]
    assert docker_block["port_mapping"] == {"5000": ""}
    assert docker_block["image"] == "registry:2"


def test_helm_releases_attached_to_k8s_entity_and_extra():
    """helm release → k8s svc 实体档案 by_runtime.helm.releases；未关联单独记录。"""
    d = discover_host("203.0.113.20", "prod", runner=_docker_runner(
        **{"kubectl": KUBE, "helm list": HELM_LIST}))
    assert d["probes"]["helm"] == "ok(2 releases)"
    # grafana svc（monitoring ns）匹配 grafana release → 挂实体档案。
    helm_block = d["details"]["grafana"]["snapshot"]["by_runtime"]["helm"]
    assert helm_block["releases"] == [{
        "name": "grafana", "namespace": "monitoring", "revision": "3",
        "status": "deployed", "chart": "grafana-8.0.0",
    }]
    # 未匹配（cilium，无同名 svc）→ 单独记录 {env}-helm-extra 实体档案。
    extra = d["details"]["prod-helm-extra"]
    assert extra["snapshot"]["by_runtime"]["helm"]["releases"] == [{
        "name": "cilium", "namespace": "kube-system", "revision": "2",
        "status": "deployed", "chart": "cilium-1.16.19",
    }]
    assert extra["detail"] == "entities/prod__203.0.113.20__prod-helm-extra.yaml"
    # k8s 服务行本身不变（managed_by kubectl，粒度不拆）。
    svc = next(s for s in d["services"] if s["name"] == "grafana")
    assert svc["managed_by"] == "kubectl"


def test_helm_unavailable_is_skipped_not_blocking():
    """helm 不存在/失败 → skipped 记录，其余探针照常，不阻塞发现。"""
    d = discover_host("203.0.113.20", "prod", runner=_docker_runner(
        **{"kubectl": KUBE, "helm list": ProbeResult("", 127)}))
    assert d["probes"]["helm"] == "skipped（helm 不可用）"
    assert {s["name"] for s in d["services"]} == {"grafana"}
    assert "prod-helm-extra" not in d["details"]


def test_helm_not_probed_when_kubectl_unavailable():
    """kubectl 不可用 → helm 探针不运行（前提不成立），probes 只记 kubectl。"""
    runner = _docker_runner(**{"kubectl": ProbeResult("", 127, "kubectl: command not found")})
    d = discover_host("203.0.113.20", "prod", runner=runner)
    assert "helm" not in d["probes"]
    assert d["probes"]["kubectl"] == "skipped（kubectl 不可用）"


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
    runner = _default_runner(**{"systemctl": SYSTEMCTL, "ss -tlnp": SS_TLNP_SYSTEMD})
    d = discover_host("203.0.113.20", "prod", runner=runner)
    names = {s["name"]: s for s in d["services"]}
    assert d["probes"]["systemctl"] == (
        "ok(2 服务，过滤 0 系统服务，另 0 个无端口系统服务未入表（可确认）)"
    )
    # v0.4：type 按判定表归类（node-exporter → monitor），managed_by=systemd；
    # unit 信息进 L3 档案 snapshot.by_runtime.systemd，不再落服务行 attrs。
    assert names["node-exporter"]["type"] == "monitor"
    assert names["node-exporter"]["managed_by"] == "systemd"
    assert d["details"]["node-exporter"]["snapshot"]["by_runtime"]["systemd"]["unit"] == {
        "load": "loaded", "active": "active", "sub": "running",
    }
    # systemctl 中出现 harbor 与 docker 容器同名 → docker 优先，不生成 systemd 行。
    assert names["harbor"]["type"] == "registry"
    assert names["harbor"]["managed_by"] == "docker_compose"
    assert "inactive" not in names
    # 有监听端口的 systemd 服务正常入表 → 无 pending，端口不重复补 unidentified。
    assert d["pending_review"] == []
    assert "unidentified-9100" not in names


SYSTEMCTL_WITH_SYSTEM_SERVICES = """\
  UNIT                     LOAD   ACTIVE SUB     DESCRIPTION
  systemd-journald.service loaded active running Journal Service
  systemd-logind.service   loaded active running Login Service
  systemd-udevd.service    loaded active running Rule Manager
  dbus.service             loaded active running D-Bus System Message Bus
  user@1000.service        loaded active running User Manager for UID 1000
  getty@tty1.service       loaded active running Getty on tty1
  sshd.service             loaded active running OpenSSH Daemon
  ssh.service              loaded active running OpenSSH Daemon (Debian)
  containerd.service       loaded active running Container Runtime
  node-exporter.service    loaded active running Prometheus Node Exporter
  prometheus.service       loaded active running Prometheus
  postgres.service         loaded active running PostgreSQL
  nginx.service            loaded active running Nginx
  redis.service            loaded active running Redis
  mysql.service            loaded active running MySQL
  inactive.service         loaded inactive dead   Not discovered
"""


def test_parse_systemctl_units_filters_system_services():
    """C1：系统内部服务（systemd-*/dbus/getty*/user@*/sshd/containerd）被过滤，
    业务服务（node-exporter/prometheus/postgres/nginx/redis/mysql）不误伤。"""
    from tools.topo_discovery import (
        _SYSTEMD_SYSTEM_SERVICES,
        _SYSTEMD_SYSTEM_SERVICE_PREFIXES,
        _count_systemd_filtered_units,
        _parse_systemctl_units,
    )

    # 黑名单常量模块级可维护：前缀 + 精确名都覆盖 prompt 点名条目
    # （ssh 为 Debian/Ubuntu 的 openssh unit 名，与 sshd 同一管理通道）。
    assert {"systemd-", "user@", "getty"} <= set(_SYSTEMD_SYSTEM_SERVICE_PREFIXES)
    assert {"dbus", "sshd", "ssh", "containerd"} <= _SYSTEMD_SYSTEM_SERVICES
    # 业务服务名绝不在黑名单里。
    assert not ({k[: -len(".service")] for k in ("postgres.service", "nginx.service",
                                                  "redis.service", "mysql.service",
                                                  "node-exporter.service",
                                                  "prometheus.service")} & _SYSTEMD_SYSTEM_SERVICES)

    units = _parse_systemctl_units(SYSTEMCTL_WITH_SYSTEM_SERVICES)
    names = {u["name"]: u for u in units}
    assert set(names) == {"node-exporter", "prometheus", "postgres", "nginx", "redis", "mysql"}
    assert all(u["unit"].endswith(".service") for u in units)
    # 过滤发生在解析层：系统服务不产出实体条目。
    assert not any(u["name"].startswith(("systemd-", "user-", "getty-")) for u in units)
    assert _count_systemd_filtered_units(SYSTEMCTL_WITH_SYSTEM_SERVICES) == 9


def test_discover_systemctl_probe_records_filter_stats():
    """C1：probes['systemctl'] 记录 `ok(N 服务，过滤 M 系统服务)` 过滤统计。"""
    runner = FakeRunner(**{
        "systemctl": SYSTEMCTL_WITH_SYSTEM_SERVICES,
        "ss -tlnp": SS_TLNP_FULL,
    })
    d = discover_host("203.0.113.20", "prod", runner=runner)
    assert d["probes"]["systemctl"] == (
        "ok(6 服务，过滤 9 系统服务，另 0 个无端口系统服务未入表（可确认）)"
    )
    names = {s["name"]: s for s in d["services"]}
    # 只有业务服务进拓扑；系统内部服务无 services/details 条目。
    assert set(names) == {"node-exporter", "prometheus", "postgres", "nginx", "redis", "mysql"}
    assert not any(n.startswith(("systemd-", "user-", "getty-")) or n in ("dbus", "sshd", "containerd")
                   for n in names)
    assert "systemd-journald" not in d["details"]
    assert "dbus" not in d["details"]
    assert d["pending_review"] == []


SYSTEMCTL_NOISE = """\
  UNIT                   LOAD   ACTIVE SUB     DESCRIPTION
  chronyd.service        loaded active running NTP client
  prometheus.service     loaded active running Prometheus
  networkmanager.service loaded active running Network Manager
"""

SS_TLNP_PROM = """\
State Recv-Q Send-Q Local Address:Port Peer Address:Port Process
LISTEN 0      128    0.0.0.0:9090  0.0.0.0:*    users:(("prometheus",pid=1,fd=3))
"""


def test_discover_systemd_no_listen_port_goes_pending_review():
    """批次十八 B 配套：无监听端口的 systemd 服务不入 services，保留 pending_review。

    有监听端口（prometheus，ss 进程名匹配）正常入表；无端口（chronyd /
    networkmanager）不入正式拓扑但保留可见性（needs_review=true，可确认）。
    """
    runner = FakeRunner(**{
        "systemctl": SYSTEMCTL_NOISE,
        "ss -tlnp": SS_TLNP_PROM,
    })
    d = discover_host("203.0.113.20", "prod", runner=runner)
    names = {s["name"]: s for s in d["services"]}
    assert "prometheus" in names
    assert names["prometheus"]["type"] == "monitor"
    assert names["prometheus"]["managed_by"] == "systemd"
    assert "chronyd" not in names
    assert "networkmanager" not in names
    # 无端口服务不进 details（不入落盘），只在 pending_review 可见。
    assert "chronyd" not in d["details"]
    pending = {s["name"]: s for s in d["pending_review"]}
    assert set(pending) == {"chronyd", "networkmanager"}
    assert all(s["needs_review"] is True for s in pending.values())
    # v0.4：type 按判定表归类（无端口系统服务兜底 app），managed_by=systemd。
    assert all(s["type"] == "app" for s in pending.values())
    assert all(s["managed_by"] == "systemd" for s in pending.values())
    # probes 文案带无端口统计（替代 LLM 手动过滤的机制保证）。
    assert "另 2 个无端口系统服务未入表（可确认）" in d["probes"]["systemctl"]
    # prometheus 的监听端口 9090 已被服务覆盖，不重复补 unidentified。
    assert "unidentified-9090" not in names


def test_discover_systemd_all_no_port_keeps_pending_only():
    """全部 systemd 服务无监听端口 → services 无 systemd 项，pending 列表有 N 条。"""
    runner = FakeRunner(**{"systemctl": SYSTEMCTL_NOISE})  # 默认 ss 无匹配进程名
    d = discover_host("203.0.113.20", "prod", runner=runner)
    assert not any(s["type"] == "systemd-service" for s in d["services"])
    pending = {s["name"] for s in d["pending_review"]}
    assert pending == {"chronyd", "prometheus", "networkmanager"}
    assert "另 3 个无端口系统服务未入表（可确认）" in d["probes"]["systemctl"]


def test_discover_systemd_docker_same_name_skipped_not_pending():
    """与 docker 已发现服务同名的 systemd unit 直接跳过（不进 pending，不重复）。"""
    runner = _default_runner(**{"systemctl": SYSTEMCTL, "ss -tlnp": SS_TLNP_SYSTEMD})
    d = discover_host("203.0.113.20", "prod", runner=runner)
    assert not any(s["name"] == "harbor" and s["managed_by"] == "systemd"
                   for s in d["services"])
    assert not any(s["name"] == "harbor" for s in d["pending_review"])


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
    # v0.4：ss 未识别端口 → pending_review（type=unknown 兜底），不占服务行。
    svc = next(s for s in d["pending_review"] if s["name"] == "unidentified-9090")
    assert svc["type"] == "unknown"
    assert svc["managed_by"] == "unknown"
    assert svc["endpoint"] == "203.0.113.20:9090"
    assert svc["needs_review"] is True
    assert "unidentified-9090" not in d["services"]


def test_build_ssh_runner_includes_identities_only(monkeypatch):
    """批次十九 §AF：探测 runner 的 ssh argv 无条件带 ``-o IdentitiesOnly=yes``。

    多 key 环境不带 IdentitiesOnly 会遍历 agent 所有 key 刷爆 MaxAuthTries
    （§AF：今天锁了 5 次 15 分钟）——探测路径与 vssh 必须同款约束。
    """
    from types import SimpleNamespace

    import tools.topo_discovery as topodisc

    calls = []

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "ssh":
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    runner = topodisc._build_ssh_runner("203.0.113.20", "root", key_path="/keys/test.pem")
    result = runner("uptime")

    assert result.ok is True
    assert calls, "runner 应调用 ssh"
    assert "-o" in calls[0] and "IdentitiesOnly=yes" in calls[0]
    assert calls[0].index("IdentitiesOnly=yes") > calls[0].index("ConnectTimeout=10")


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
        "203.0.113.20", "root", key_path="/keys/test.pem", sudo_password_file=askpass
    )
    result = runner("docker ps")

    assert result.ok is True
    assert "sudo -S -p '' docker ps" in calls["argv"][-1]
    assert calls["kwargs"]["input"] == sudo_secret + "\n"
    blob = json.dumps(calls["argv"]) + json.dumps(calls["kwargs"].get("env", {}))
    assert sudo_secret not in blob


def test_discover_localhost_uses_local_runner_no_ssh_argv(monkeypatch):
    """C4：host=localhost → 探测直接本地 subprocess 执行（shell 命令串，无 ssh argv）。"""
    from types import SimpleNamespace

    import tools.topo_discovery as topodisc

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=127, stdout="", stderr="")

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    d = discover_host("localhost", "prod", {"user": "ops", "key_path": "/keys/x.pem"})

    assert calls, "本地 runner 应执行探测命令"
    for cmd, kwargs in calls:
        assert isinstance(cmd, str), "本地执行用 shell 命令串，不经 ssh argv"
        assert "ssh" not in cmd
        assert kwargs.get("shell") is True
    assert d["host"]["endpoint"] == "localhost"
    # v0.4：本机主机行名用本机 hostname（保大小写，与第一层手动登记行合并命中）。
    assert d["host"]["name"] != "localhost"
    assert d["host"]["name"] == socket.gethostname()
    # 本机发现不走 SSH → 不写 ssh_key 凭据引用。
    assert "credential" not in d["host"]
    assert "credentials" not in d["host"]


def test_discover_empty_host_defaults_to_localhost(monkeypatch):
    """C4：host 省略（空）→ 同样走本地 runner，endpoint 归一为 localhost。"""
    from types import SimpleNamespace

    import tools.topo_discovery as topodisc

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=127, stdout="", stderr="")

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    d = discover_host("", "local")
    assert d["host"]["endpoint"] == "localhost"
    assert calls and all(isinstance(c, str) and "ssh" not in c for c in calls)


def test_discover_remote_host_still_uses_ssh(monkeypatch):
    """C4：非 localhost（10.0.0.5）→ 仍走 SSH runner（argv[0] == ssh）。"""
    from types import SimpleNamespace

    import tools.topo_discovery as topodisc

    calls = []

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "ssh":
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected local argv: {argv}")

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    d = discover_host("10.0.0.5", "prod", {"user": "root", "key_path": "/keys/x.pem"})
    assert calls and calls[0][0] == "ssh"
    assert f"root@10.0.0.5" in calls[0]
    assert d["host"]["endpoint"] == "10.0.0.5"


def test_build_local_runner_sudo_password_uses_stdin(tmp_path, monkeypatch):
    """C4：本地 sudo 复用 sudo_password_file → sudo -S 从 stdin 注入，命令串无明文。"""
    from types import SimpleNamespace

    import tools.topo_discovery as topodisc

    sudo_secret = "sudo-secret-88"
    vault = tmp_path / "vault"
    vault.write_text(sudo_secret, encoding="utf-8")
    askpass = topodisc._make_askpass_script(vault)
    calls = {}

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["/bin/sh", str(askpass)]:
            return SimpleNamespace(returncode=0, stdout=sudo_secret + "\n", stderr="")
        if isinstance(cmd, str) and cmd.startswith("sudo "):
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    runner = topodisc._build_local_runner(sudo_password_file=askpass)
    result = runner("docker ps")

    assert result.ok is True
    assert calls["cmd"] == "sudo -S -p '' docker ps"
    assert calls["kwargs"]["input"] == sudo_secret + "\n"
    # 密码只在 stdin 注入；命令串/env 无明文。
    blob = json.dumps(calls["cmd"]) + json.dumps(calls["kwargs"].get("env", {}))
    assert sudo_secret not in blob


def test_cli_host_omitted_defaults_to_localhost(tmp_path, monkeypatch, capsys):
    """C4：host 省略 → CLI 默认本机发现，不要求凭据、不报错。"""
    import hermes_cli.topo_discover as td

    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(home))
    seen = {}

    def fake_discover(host, env, creds, **kwargs):
        seen["host"] = host
        seen["creds"] = creds
        return _discovery("localhost")

    def fake_prompt(*a, **kw):
        raise AssertionError("本机发现不应调用 _prompt_credentials")

    monkeypatch.setattr(td, "_prompt_credentials", fake_prompt)
    monkeypatch.setattr(td, "discover_host", fake_discover)
    monkeypatch.setattr(td, "write_discovery", lambda *a, **kw: {"written": []})

    rc = td.main(["--env", "prod", "--dry-run", "--yes"])
    assert rc == 0
    assert seen["host"] == "localhost"
    assert seen["creds"] == {}
    out = capsys.readouterr().out
    assert "localhost" in out


def test_cli_localhost_skips_ssh_credential_prompt(tmp_path, monkeypatch):
    """C4：--host localhost 无 --user/--key → 不报错，凭据为空，直接本机发现。"""
    import hermes_cli.topo_discover as td

    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(home))
    seen = {}

    def fake_discover(host, env, creds, **kwargs):
        seen["host"] = host
        seen["creds"] = creds
        return _discovery("localhost")

    def fake_prompt(*a, **kw):
        raise AssertionError("localhost 不应走 SSH 凭据交互")

    monkeypatch.setattr(td, "_prompt_credentials", fake_prompt)
    monkeypatch.setattr(td, "discover_host", fake_discover)
    monkeypatch.setattr(td, "write_discovery", lambda *a, **kw: {"written": []})

    rc = td.main(["--host", "localhost", "--env", "prod", "--dry-run", "--yes"])
    assert rc == 0
    assert seen["host"] == "localhost"
    assert seen["creds"] == {}


def test_cli_remote_host_still_prompts_credentials(tmp_path, monkeypatch):
    """C4：非 localhost 仍走 _prompt_credentials（SSH 路径不变）。"""
    import hermes_cli.topo_discover as td

    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(home))
    seen = {}

    def fake_prompt(host, user, key, **kwargs):
        seen["host"] = host
        return {"user": user, "key_path": key}

    monkeypatch.setattr(td, "_prompt_credentials", fake_prompt)
    monkeypatch.setattr(td, "discover_host", lambda *a, **kw: _discovery())
    monkeypatch.setattr(td, "write_discovery", lambda *a, **kw: {"written": []})

    rc = td.main(["--host", "10.0.0.5", "--env", "prod", "--dry-run", "--yes"])
    assert rc == 0
    assert seen["host"] == "10.0.0.5"


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

    assert td._expand_hosts(["203.0.113.23[3-8]"]) == [
        "203.0.113.233", "203.0.113.234", "203.0.113.235",
        "203.0.113.236", "203.0.113.237", "203.0.113.238",
    ]
    assert td._expand_hosts(["203.0.113.233,203.0.113.234"]) == [
        "203.0.113.233", "203.0.113.234",
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

    rc = td.main(["--host", "203.0.113.23[3-8]", "--env", "prod", "--dry-run", "--yes"])
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

    rc = td.main(["--host", "203.0.113.23[3-8]", "--env", "prod", "--yes"])
    assert rc == 0
    assert confirm_calls == [1]
    assert len(writes) == 6


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

def _discovery(host: str = "203.0.113.20", env: str = "prod"):
    return discover_host(host, env, runner=_default_runner())


def test_write_discovery_writes_v4_structure(tmp_path):
    home = tmp_path / "hermes_home"
    home.mkdir()
    d = _discovery()
    result = write_discovery(home, d)
    assert result["written"]

    # v0.4：第二层目录 services/<host>.yaml（不再有 hosts/）。
    index = home / "services" / "203.0.113.20.yaml"
    assert index.is_file()
    data = yaml.safe_load(index.read_text(encoding="utf-8"))
    assert data["host"] == "203.0.113.20"
    assert data["updated_at"]
    # unidentified 端口在 pending_review，不入服务索引 → 3 条。
    assert {s["name"] for s in data["services"]} == {
        "harbor", "db", "grafana"}

    topo = yaml.safe_load((home / "topology.yaml").read_text(encoding="utf-8"))
    assert topo["version"] == 4
    assert "sources" not in topo and "cross_host" not in topo and "key_paths" not in topo
    assert topo["hosts"][0]["name"] == "203.0.113.20"
    assert topo["hosts"][0]["cluster"] == "default"
    assert topo["hosts"][0]["role"] == ["docker-host"]
    assert topo["hosts"][0]["runtime"] == ["docker"]
    assert "services_index" not in topo["hosts"][0]

    # L3 命名：entities/{env}__{host}__{name}.yaml（cluster 空 → env 兜底），
    # 档案带 detail 路径键 + snapshot 结构。
    harbor = home / "entities" / "prod__203.0.113.20__harbor.yaml"
    assert harbor.is_file()
    entity = yaml.safe_load(harbor.read_text(encoding="utf-8"))
    assert entity["name"] == "harbor"
    assert entity["detail"] == "entities/prod__203.0.113.20__harbor.yaml"
    assert entity["snapshot"]["captured_at"] and entity["snapshot"]["source"] == "discovered"
    assert entity["snapshot"]["by_runtime"]["docker_compose"]["project"] == "harbor"

    # 硬件层 hardware/<host>.yaml 一并落盘。
    hw = home / "hardware" / "203.0.113.20.yaml"
    assert hw.is_file()
    hw_data = yaml.safe_load(hw.read_text(encoding="utf-8"))
    assert hw_data["host"] == "203.0.113.20"
    assert hw_data["hardware"]["gpu"]["controller"] == "nvidia-smi"
    assert hw_data["network"]["firewall"]["controller"] == "none"

    # 落盘结果能被 topo_tools 正常读取（第二层服务索引进扁平视图）。
    os.environ["VIGIL_HOME"] = str(home)
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        from tools.topo_tools import topo_query
        node = json.loads(topo_query(host="203.0.113.20"))
        assert {s["name"] for s in node["services"]} >= {"harbor", "db"}
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


def test_write_discovery_existing_host_merges_unless_force(tmp_path):
    """批次十八 B：已存在 host 重扫默认合并（不拒绝）；--force 整体替换。"""
    home = tmp_path / "hermes_home"
    home.mkdir()
    d = _discovery()
    write_discovery(home, d)

    # 默认 merge=True：重扫同一结果 → 全部同名跳过，不报错，行数不变。
    result = write_discovery(home, d)
    assert result["merged"] is True
    assert result["appended"] == 0
    assert result["kept"] == 3

    # merge=False（显式关闭合并）→ 维持旧语义：拒绝，提示 --force。
    with pytest.raises(DiscoveryError) as exc:
        write_discovery(home, d, merge=False)
    assert "--force" in str(exc.value)

    # --force → 整体替换（appended 全量、merged=False）。
    result = write_discovery(home, d, force=True)
    assert result["written"]
    assert result["merged"] is False
    assert result["appended"] == 3
    assert result["kept"] == 0


def test_write_discovery_merges_existing_host_appends_new_keeps_manual(tmp_path):
    """批次十八 B：已有 host 重扫 → 新服务追加进索引+entities，手动实体保留。"""
    home = tmp_path / "hermes_home"
    home.mkdir()
    d = _discovery()
    r1 = write_discovery(home, d)
    assert r1["appended"] == 3 and r1["kept"] == 0 and r1["merged"] is False

    # 手动维护：往索引加一条 app 手动实体（v0.4 服务行：无 env/cluster 冗余，
    # type 用受控枚举 app + managed_by）。
    index_path = home / "services" / "203.0.113.20.yaml"
    data = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    data["services"].append({
        "name": "app", "type": "app", "managed_by": "bare",
        "endpoint": "203.0.113.20:8080", "source": "manual", "needs_review": False,
        "detail": "entities/prod__203.0.113.20__app.yaml",
    })
    index_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    # 重扫：发现结果比现有索引多一个 redis 服务 → 只追加 redis。
    d2 = dict(d)
    d2["services"] = list(d["services"]) + [{
        "name": "redis", "type": "cache", "managed_by": "docker_compose",
        "endpoint": "203.0.113.20:6379", "source": "discovered",
        "last_verified": "2026-08-14", "needs_review": True,
        "detail": "entities/prod__203.0.113.20__redis.yaml",
    }]
    d2["details"] = dict(d["details"])
    d2["details"]["redis"] = {
        "name": "redis", "detail": "entities/prod__203.0.113.20__redis.yaml",
        "version": "7", "updated_at": "2026-08-14",
        "checks": [],
        "snapshot": {
            "captured_at": "2026-08-14T00:00:00+00:00", "source": "discovered",
            "common": {"version": "7", "config_dir": "", "log_dir": "", "data_dir": "", "mode": "single"},
            "by_type": {"persistence": "none"},
            "by_runtime": {"docker_compose": {"project": "redis", "workdir": "", "services": []}},
        },
        "notes": "",
    }
    r2 = write_discovery(home, d2)
    assert r2["merged"] is True
    assert r2["appended"] == 1
    assert r2["kept"] == 4

    data2 = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    names = {s["name"]: s for s in data2["services"]}
    assert names["app"]["endpoint"] == "203.0.113.20:8080"   # 手动行保留
    assert names["app"]["source"] == "manual"                # 手动行不被覆盖
    assert names["app"]["needs_review"] is False
    assert names["redis"]["endpoint"] == "203.0.113.20:6379"  # 新服务追加
    assert names["harbor"]["endpoint"] == "203.0.113.20:30443"  # 原自动行保留
    # redis 实体文件新写入；app 手动实体文件不被创建/覆盖。
    assert (home / "entities" / "prod__203.0.113.20__redis.yaml").is_file()
    assert not (home / "entities" / "prod__203.0.113.20__app.yaml").exists()


def test_write_discovery_new_host_append_unchanged(tmp_path):
    """新 host 追加（现状行为不变）：互不干扰、topology hosts 段逐条追加。"""
    home = tmp_path / "hermes_home"
    home.mkdir()
    r1 = write_discovery(home, _discovery("node-a"))
    r2 = write_discovery(home, _discovery("node-b"))
    assert r1["merged"] is False and r2["merged"] is False
    assert r1["appended"] == 3 and r2["appended"] == 3
    topo = yaml.safe_load((home / "topology.yaml").read_text(encoding="utf-8"))
    assert [h["name"] for h in topo["hosts"]] == ["node-a", "node-b"]


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
    assert not (home / "services").exists()


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


def test_cli_write_prompt_contains_three_step_review_guidance(tmp_path, monkeypatch, capsys):
    """批次十二 C2：落盘后提示给具体三步 review 指引（查看/确认/效果），不抽象说请核对。"""
    import hermes_cli.topo_discover as td

    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(home))
    monkeypatch.setattr(td, "discover_host", lambda *a, **kw: _discovery())
    monkeypatch.setattr(td, "write_discovery", lambda *a, **kw: {"written": ["hosts/node1.yaml"]})
    monkeypatch.setattr(td, "_prompt_credentials", lambda host, user, key: {"user": "root"})

    rc = td.main(["--host", "203.0.113.20", "--env", "prod", "--yes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "needs_review" in out
    assert "1. 查看" in out and "2. 确认" in out and "3. 效果" in out
    assert "topo_update" in out and "needs_review=false" in out
    assert "未经确认不参与权限判定" in out


def test_parse_docker_ps_names_array_form():
    """docker --format '{{json .}}' 的 Names 是 JSON 数组（["/app"]）——必须取首元素。

    回归：2026-08-14 批次二十四验收实锤——fixture 用字符串形态（"Names":"harbor"）
    掩盖了数组形态 bug（str() 把数组转成 "['/app']"），导致容器名匹配失败、
    topo_status_sync 全落 ask。真实 docker 输出是数组。
    """
    from tools.topo_discovery import _parse_docker_ps

    # 数组形态（真实 docker 输出）
    out = '{"Names":["/app"],"State":"exited"}\n'
    containers = _parse_docker_ps(out)
    assert containers and containers[0]["name"] == "app", containers
    assert containers[0]["state"] == "exited"

    # 字符串形态（兼容既有 fixture）
    out2 = '{"Names":"/db","State":"running"}\n'
    containers2 = _parse_docker_ps(out2)
    assert containers2 and containers2[0]["name"] == "db", containers2

    # 空 Names
    out3 = '{"Names":[],"State":"running"}\n'
    assert _parse_docker_ps(out3) == []
