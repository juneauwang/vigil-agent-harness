"""词表层 schemas.yaml（YAPL P1，OPS-DELTA #67）——受控枚举 + 配置化演进。

设计（yapl-design.md §9.6/§9.7）：
- 枚举源下沉配置层（不锁死在 py enum）——加值 = 编辑 YAML（+ 可选 handler），
  零发版；文件 ``~/.vigil/schemas.yaml``（``ops.schemas.*``），缺省用内置表；
- LLM 填受控值（工具 schema / 示例注入 / 报错引导三件套），不开放自由文本；
  ``unknown`` 兜底过渡（发现/校验器兜底）；
- managed_by 加值贵（执行器 handler 必须同步）——枚举克制，执行器支持不了的
  不放枚举（``managed_by`` 值 → 命令家族映射供执行器用）。

只读模块：不写拓扑、不执行命令；导出初始文件由调用方显式触发
（``export_schemas_yaml``，数据重建步骤用）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

_SCHEMAS_FILENAME = "schemas.yaml"

# 内置默认枚举表（yapl-design.md §9.7 枚举总表）。配置文件的 ``ops.schemas``
# 逐键覆盖；未配置的键回落到这里。
_DEFAULT_SCHEMAS: Dict[str, Any] = {
    "schema_version": 1,
    # service.type / entity type（§9.7：scheduler 已删，jenkins/airflow 归 app，
    # cron 不登记，k8s cronjob 非服务）。
    "entity_types": [
        "db", "cache", "queue", "registry", "monitor", "gateway", "search",
        "object_storage", "app", "unknown",
    ],
    "cluster_types": ["k8s", "k3s", "kind", "docker", "bare"],
    "host_roles": ["control-plane", "worker", "edge", "docker-host", "standalone"],
    "host_runtimes": ["bare", "docker", "containerd", "k3s", "k8s", "podman"],
    "source": ["manual", "discovered", "agent", "monitor"],
    "provenance": ["terraform", "ansible", "salt", "manual"],
    # managed_by 值 → 命令家族（执行器映射）；None = 无默认命令（bare/unknown）。
    "managed_by": {
        "docker": "docker",
        "docker_compose": "docker compose",
        "kubectl": "kubectl",
        "helm": "helm",
        "systemd": "systemctl",
        "pm2": "pm2",
        "supervisord": "supervisorctl",
        "bare": None,
        "unknown": None,
    },
    # runbook 动作词表（yapl-design.md §10.2：8 族；逐个数 23 个，标题"24"
    # 以文本为准，差异在 OPS-DELTA #68 登记）。v0.1 runbook 不消费该词表
    # （steps 用 commands），仅 v0.2 runbook 校验动作合法性。
    # 动作未知不 unknown 兜底——动作是执行的，未知动作无法执行（校验器报错）。
    "actions": [
        # 生命周期（6）：start/stop/restart/reload {target, batch?, interval?, timeout?,
        #   force?(stop)}；enable/disable {target}
        "start", "stop", "restart", "reload", "enable", "disable",
        # 主机（2）：reboot/shutdown {target, batch?, interval?, timeout?}
        "reboot", "shutdown",
        # 发布（4）：deploy {target, image?, version?, batch?, interval?}
        #   rollback {target, to?}；scale {target, replicas(必填)}；decommission {target}
        "deploy", "rollback", "scale", "decommission",
        # 数据（2）：backup {target, dest, batch?}；restore {target, from, batch?}
        "backup", "restore",
        # 配置（1）：apply_config {target, changes: [{key(必填), value?}], batch?, interval?}
        "apply_config",
        # 查询（3）：query {target?, pattern?}；fetch_log {target, lines?, grep?, since?}
        #   verify {target}
        "query", "fetch_log", "verify",
        # 文件（1）：transfer_file {source: {host?, path}, dest: {host?, path}}
        "transfer_file",
        # 执行（1）：run_script {script(资产引用), args?}
        "run_script",
        # 包（3）：install/upgrade {target, package, version?, repo?}
        #   remove {target, package, deps?(默认 false)}
        "install", "upgrade", "remove",
    ],
    # YAPL P5（OPS-DELTA #75）：操作分类层规则表可配置化——schemas.yaml
    # ``ops.schemas.command_rules``（[{pattern, action, note}]，按序命中、长模式
    # 先）覆盖 tools/action_classifier.py 内置表；缺省 [] = 用内置表（本键只为
    # 让配置文件里的 command_rules 进入白名单合并）。
    "command_rules": [],
    "service_status": ["running", "restarting", "exited", "failed"],
    "raid_tool": ["ssacli", "storcli", "megacli", "mdadm", "perccli", "none"],
    "gpu_controller": ["nvidia-smi", "npu-smi", "cambricon-smi", "rocm-smi", "none"],
    "firewall_controller": ["firewalld", "iptables", "ufw", "nftables", "none"],
    "db_role": ["primary", "replica", "standalone"],
    "cache_persistence": ["rdb", "aof", "both", "none"],
    "monitor_collection_mode": ["scrape", "push", "passive", "api"],
}

# 可被配置文件覆盖的键（白名单：未知键不吞进运行时表，防拼写错误静默生效）。
_SCHEMA_KEYS = frozenset(_DEFAULT_SCHEMAS.keys())


def default_schemas() -> Dict[str, Any]:
    """内置默认枚举表（深拷贝，调用方可安全改动）。"""
    return {k: (dict(v) if isinstance(v, dict)
                 else list(v) if isinstance(v, (list, tuple)) else v)
            for k, v in _DEFAULT_SCHEMAS.items()}


def schemas_path(home: Optional[Path] = None) -> Path:
    from hermes_constants import get_hermes_home
    return Path(home or get_hermes_home()) / _SCHEMAS_FILENAME


def load_schemas(home: Optional[Path] = None) -> Dict[str, Any]:
    """读 ``~/.vigil/schemas.yaml`` 并合并内置默认表（缺省 → 纯内置表）。

    配置文件形态：::

        ops:
          schemas:
            entity_types: [...]
            managed_by: {docker: docker, ...}

    只合并白名单键；坏文件/缺文件 → 内置表（日志告警，绝不抛）。
    """
    merged = default_schemas()
    path = schemas_path(home)
    if not path.is_file():
        return merged
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("topo_schemas: failed to parse %s: %s", path, exc)
        return merged
    ops = (data or {}).get("ops") or {}
    schemas = ops.get("schemas") if isinstance(ops, dict) else None
    if not isinstance(schemas, dict):
        return merged
    for key, value in schemas.items():
        if key not in _SCHEMA_KEYS:
            logger.warning("topo_schemas: 未知 schema 键被忽略: %r", key)
            continue
        merged[key] = value
    return merged


def schema_list(name: str, home: Optional[Path] = None) -> Optional[List[str]]:
    """取枚举列表（managed_by 等 dict 形态返回其键列表）；未知名 → None。"""
    schemas = load_schemas(home)
    value = schemas.get(name)
    if isinstance(value, dict):
        return [str(k) for k in value.keys()]
    if isinstance(value, list):
        return [str(v) for v in value]
    return None


def validate_enum(value: Any, name: str, home: Optional[Path] = None,
                  fallback: str = "unknown") -> str:
    """受控枚举校验：合法值原样返回；非法/缺省 → fallback（默认 unknown）。

    ``unknown`` 兜底是设计铁律：LLM/发现探针给的自由文本不得溜进受控词表。
    """
    allowed = schema_list(name, home)
    text = str(value or "").strip()
    if allowed is not None:
        if text in allowed:
            return text
        if text in ("", "-", "?"):
            return fallback
        logger.debug("topo_schemas: %s=%r 不在枚举 %s，兜底 %r", name, text, allowed, fallback)
        return fallback
    return text or fallback


def validate_enum_list(values: Any, name: str, home: Optional[Path] = None,
                       fallback: Optional[str] = None) -> List[str]:
    """数组形态枚举校验：逐项过滤非法值；空结果回落到 fallback（None = 保留空）。"""
    allowed = set(schema_list(name, home) or [])
    out: List[str] = []
    for item in values or []:
        text = str(item or "").strip()
        if allowed and text in allowed:
            out.append(text)
    if not out and fallback:
        return [fallback]
    return out


def managed_by_command(name: str, home: Optional[Path] = None) -> Optional[str]:
    """managed_by 值 → 命令家族（执行器映射；bare/unknown → None）。"""
    schemas = load_schemas(home)
    table = schemas.get("managed_by") or {}
    if not isinstance(table, dict):
        return None
    return table.get(str(name or "")) or None


def export_schemas_yaml(home: Optional[Path] = None, force: bool = False) -> Optional[Path]:
    """把内置默认枚举表导出为 ``~/.vigil/schemas.yaml``（数据重建步骤）。

    已存在且未 ``force`` → 不覆盖（返回 None；用户自定义演进不被冲掉）。
    """
    path = schemas_path(home)
    if path.is_file() and not force:
        return None
    payload = {
        "# 词表层 schemas.yaml（YAPL v1.0 P1）": None,
        "# 受控枚举 + 配置化演进：加值 = 编辑本文件（managed_by 加值还需执行器 handler）": None,
        "ops": {"schemas": default_schemas()},
    }
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    text = "\n".join(
        line for line in text.splitlines()
        if not (line.strip().startswith("'#") and line.strip().endswith(": null"))
    )
    header = (
        "# schemas.yaml —— 词表层：受控枚举（YAPL v1.0 P1，yapl-design.md §9.6/§9.7）\n"
        "# 枚举源下沉配置层：加值 = 编辑本文件（+ 可选 handler），零发版。\n"
        "# managed_by 加值贵（执行器 handler 必须同步）——枚举克制。\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + text, encoding="utf-8")
    return path
