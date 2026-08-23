"""YAPL P5 操作分类层（yapl-design.md §11.1/§11.6）——terminal 命令串 → 动作枚举。

所有执行路径（runbook 步骤 / terminal 直跑 / 工具调用）归一：classifier（terminal）
→ 操作矩阵 → 审批门。本模块是 terminal 直跑的收口：命令/意图 → 动作枚举
（schemas.yaml actions 23 个），规则表优先、识别不出默认保守。

设计铁律（P5 任务书，不要违背）：
- 词面绕过消失：``docker ps | grep harbor`` → 按主命令 docker ps 分类 = query
  （粗粒度语义分类，管道只影响"命令成功标准"不影响动作分类）。
- 链式取保守：``a && b`` / ``a ; b`` → 拆分子命令各自分类，矩阵档位取最高
  （保守）。档位比较在审批层（check_ops_command_permission / approval.py）按
  矩阵做；本模块 ``action`` 字段对链式返回静态档位最保守的候选（展示用），
  ``chain`` 字段携带全部子命令分类供矩阵层比较。
- 识别不出 → ``unknown`` → 矩阵漏配 → 默认 approve（保守，走审批门）+ warning。
- 矩阵无 deny：classifier 只产出动作枚举，不产出 deny；保守 = approve 不是拒绝。
- classifier 是内部执行路径，不是 LLM 工具（LLM 不该关心分类，它只该知道
  "terminal 命令走同一矩阵"）——不注册 model tool（OPS-DELTA #75 登记）。

规则表可配置化：schemas.yaml ``ops.schemas.command_rules``（[{pattern, action,
note}]，按序命中——长模式先）覆盖内置表（OPS-DELTA #75 登记）；缺省用本模块
内置规则表（_DEFAULT_RULES）。命令前缀 sudo/doas 剥离后分类。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ACTION_UNKNOWN = "unknown"

# 链式操作符：&& / || / ; / 换行（保守拆解）；管道不是链式操作符（主命令分类，
# | grep 等只影响"命令成功标准"不影响动作分类）。
_CHAIN_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;)\s*|\n+")
_PIPE_SPLIT_RE = re.compile(r"\s*\|\s*")

# sudo/doas 前缀剥离：sudo [-i] [-u user] [-E] [-H] <command>。取值 flag
# （-u/-p/-C/-D/-g/-R/-r/-t/-T/-U）多剥一个值 token；布尔 flag（-i/-H/-E/-n
# 等）只剥自身，避免把真实命令误吞成 flag 值（sudo -i docker ps → docker ps）。
_SUDO_TOKEN_RE = re.compile(r"^(?:sudo|doas)\b")
_SUDO_VALUE_FLAGS = frozenset("CDgpRrtTUu")
_SUDO_SHELL_WORD_RE = re.compile(r"^(?:[^\s'\"]+|'[^']*'|\"[^\"]*\")")

# 静态档位保守序（链式展示用；矩阵层按矩阵档位取最高，本表只做展示/回退）。
_ACTION_RANK: Dict[str, int] = {
    "query": 0, "fetch_log": 0, "verify": 0,
    "backup": 1,
    "enable": 2, "disable": 2,
    "start": 3, "stop": 3, "reload": 3, "transfer_file": 3,
    "restart": 4,
    "install": 5, "upgrade": 5, "apply_config": 5,
    "run_script": 6, "scale": 6,
    "deploy": 7, "rollback": 7,
    "restore": 8,
    "remove": 9, "decommission": 9,
    "reboot": 10, "shutdown": 10,
    ACTION_UNKNOWN: 11,
}

# 内置规则表（pattern, action, rule_id, note）。按序匹配——长模式/更具体的
# 规则在前（docker compose 先于 docker、kubectl rollout 先于 kubectl）。
_DEFAULT_RULES: List[Tuple[str, str, str, str]] = [
    # ---- 受控通道（§八待办收口：ssh/scp/rsync 走 vssh/拓扑凭据）----
    # 必须在命令族规则之前：ssh 串里可能内嵌 systemctl/docker 等命令，受控通道
    # 语义优先（动作 run_script/transfer_file，note 提示走受控通道）。
    (r"^ssh\b", "run_script", "ssh.controlled",
     "ssh 类走 vssh/拓扑凭据受控通道（§八待办收口），不裸连直跑"),
    (r"^scp\b", "transfer_file", "scp.transfer",
     "scp 走 transfer_file 语义（受控通道）"),
    (r"^rsync\b", "transfer_file", "rsync.transfer",
     "rsync 走 transfer_file 语义（受控通道）"),
    (r"^sftp\b", "transfer_file", "sftp.transfer",
     "sftp 走 transfer_file 语义（受控通道）"),
    # ---- docker compose（先于通用 docker，长模式先）----
    (r"docker\s+compose\s+up\b", "deploy", "docker.compose.up",
     "compose 服务启动/更新（deploy 语义）"),
    (r"docker\s+compose\s+down\b", "decommission", "docker.compose.down",
     "compose 服务拆除（decommission 语义）"),
    (r"docker\s+compose\s+restart\b", "restart", "docker.compose.restart", ""),
    (r"docker\s+compose\s+start\b", "start", "docker.compose.start", ""),
    (r"docker\s+compose\s+stop\b", "stop", "docker.compose.stop", ""),
    (r"docker\s+compose\s+rm\b", "remove", "docker.compose.rm", ""),
    (r"docker\s+compose\s+build\b", "install", "docker.compose.build",
     "镜像构建（install 语义，矩阵档位管）"),
    (r"docker\s+compose\s+pull\b", "install", "docker.compose.pull",
     "镜像拉取（install 语义）"),
    # ---- docker 通用 ----
    (r"docker\s+restart\b", "restart", "docker.restart", ""),
    (r"docker\s+start\b", "start", "docker.start", ""),
    (r"docker\s+stop\b", "stop", "docker.stop", ""),
    (r"docker\s+reload\b", "reload", "docker.reload", ""),
    (r"docker\s+logs\b", "fetch_log", "docker.logs", ""),
    (r"docker\s+(?:ps|inspect|stats|port|images|image\s+ls|network\s+ls|"
     r"volume\s+ls|info|version)\b", "query", "docker.query", ""),
    (r"docker\s+build\b", "install", "docker.build", "镜像构建（install 语义）"),
    (r"docker\s+pull\b", "install", "docker.pull", "镜像获取（install 语义）"),
    (r"docker\s+push\b", "install", "docker.push", "镜像推送（install 语义）"),
    (r"docker\s+rmi?\b", "remove", "docker.remove", ""),
    # ---- kubectl ----
    (r"kubectl\s+rollout\s+restart\b", "restart", "kubectl.rollout_restart", ""),
    (r"kubectl\s+rollout\s+undo\b", "rollback", "kubectl.rollout_undo", ""),
    (r"kubectl\s+set\s+image\b", "deploy", "kubectl.set_image", ""),
    (r"kubectl\s+apply\b", "deploy", "kubectl.apply", ""),
    (r"kubectl\s+scale\b", "scale", "kubectl.scale", ""),
    (r"kubectl\s+delete\b", "decommission", "kubectl.delete", ""),
    (r"kubectl\s+logs\b", "fetch_log", "kubectl.logs", ""),
    (r"kubectl\s+(?:get|describe|top|api-resources|version)\b", "query",
     "kubectl.query", ""),
    # ---- systemctl / service（systemctl 全族 → 对应动作）----
    (r"systemctl\s+restart\b", "restart", "systemctl.restart", ""),
    (r"systemctl\s+start\b", "start", "systemctl.start", ""),
    (r"systemctl\s+stop\b", "stop", "systemctl.stop", ""),
    (r"systemctl\s+reload\b", "reload", "systemctl.reload", ""),
    (r"systemctl\s+enable\b", "enable", "systemctl.enable", ""),
    (r"systemctl\s+disable\b", "disable", "systemctl.disable", ""),
    (r"systemctl\s+status\b", "query", "systemctl.status", ""),
    (r"systemctl\s+reboot\b", "reboot", "systemctl.reboot", ""),
    (r"systemctl\s+poweroff\b", "shutdown", "systemctl.poweroff", ""),
    (r"service\s+[^\s]+\s+restart\b", "restart", "service.restart", ""),
    (r"service\s+[^\s]+\s+start\b", "start", "service.start", ""),
    (r"service\s+[^\s]+\s+stop\b", "stop", "service.stop", ""),
    (r"service\s+[^\s]+\s+reload\b", "reload", "service.reload", ""),
    # ---- 包管理 ----
    (r"\b(?:apt|apt-get)\s+install\b", "install", "apt.install", ""),
    (r"\b(?:apt|apt-get)\s+(?:remove|purge)\b", "remove", "apt.remove", ""),
    (r"\b(?:apt|apt-get)\s+upgrade\b", "upgrade", "apt.upgrade", ""),
    (r"\b(?:dnf|yum)\s+install\b", "install", "dnf.install", ""),
    (r"\b(?:dnf|yum)\s+(?:remove|purge)\b", "remove", "dnf.remove", ""),
    (r"\b(?:dnf|yum)\s+upgrade\b", "upgrade", "dnf.upgrade", ""),
    (r"\b(?:pip|pip3)\s+install\b", "install", "pip.install", ""),
    (r"\b(?:pip|pip3)\s+uninstall\b", "remove", "pip.remove", ""),
    # ---- pm2 ----
    (r"pm2\s+restart\b", "restart", "pm2.restart", ""),
    (r"pm2\s+start\b", "start", "pm2.start", ""),
    (r"pm2\s+stop\b", "stop", "pm2.stop", ""),
    # ---- helm ----
    (r"helm\s+install\b", "deploy", "helm.install", ""),
    (r"helm\s+upgrade\b", "upgrade", "helm.upgrade", ""),
    (r"helm\s+(?:uninstall|delete)\b", "decommission", "helm.uninstall", ""),
    (r"helm\s+rollback\b", "rollback", "helm.rollback", ""),
    # ---- ansible（+ P2 inventory 契约继续校验内容）----
    (r"\bansible-playbook\b", "deploy", "ansible.playbook",
     "playbook 执行（deploy 语义；P2 inventory 契约继续校验内容）"),
    (r"\bansible\b(?!-)", "deploy", "ansible.ad_hoc",
     "ansible 命令（deploy 语义；inventory 契约继续校验内容）"),
    # ---- curl/wget（-o/-O/--output → transfer_file 语义边界；探测 → query）----
    (r"\bcurl\b[^\n]*\s(?:-o|-O|--output)\s+\S+", "transfer_file",
     "curl.download", "curl 下载文件（transfer_file 语义边界）"),
    (r"\bwget\b[^\n]*\s-O\b", "transfer_file", "wget.download",
     "wget 下载文件（transfer_file 语义边界）"),
    (r"\bcurl\b[^\n]*\s(?:-X\s+[A-Z]+|-d|--data|-F|--form|--upload-file)\b",
     ACTION_UNKNOWN, "curl.write", "curl 写操作未识别（保守 unknown，走审批门）"),
    (r"\bcurl\b", "query", "curl.probe", "curl 探测（query/verify）"),
    (r"\bwget\b", "query", "wget.probe", "wget 探测（query）"),
    # ---- 备份 / 恢复族 ----
    (r"\bpg_dump\b", "backup", "backup.pg_dump", ""),
    (r"\bmysqldump\b", "backup", "backup.mysqldump", ""),
    (r"\bpg_restore\b", "restore", "restore.pg_restore", ""),
    (r"\btar\b[^\n]*\s-?c[^\s]*f\b", "backup", "backup.tar_create",
     "tar 打包（backup 语义）"),
    (r"\btar\b[^\n]*\s-?x[^\s]*f\b", "restore", "restore.tar_extract",
     "tar 解包（restore 语义）"),
    # ---- 主机生命周期 ----
    (r"\breboot\b", "reboot", "host.reboot", ""),
    (r"\bshutdown\b", "shutdown", "host.shutdown", ""),
]

_UNKNOWN_NOTE = "未能识别命令意图，按保守审批处理；可在 OPS-DELTA 登记新规则"

# 编译规则缓存（按 schemas.yaml 路径 + mtime；测试改 VIGIL_HOME 后自动重建）。
_rules_cache: Dict[Tuple[str, float], List[Tuple[re.Pattern, str, str, str]]] = {}


def _valid_actions(home: Optional[str] = None) -> Optional[List[str]]:
    try:
        from tools.topo_schemas import schema_list
        return schema_list("actions", home)
    except Exception:
        return None


def _load_configured_rules(home: Optional[str] = None) -> Optional[List[Tuple[str, str, str, str]]]:
    """schemas.yaml ops.schemas.command_rules 覆盖内置表（[{pattern, action, note}]）。"""
    try:
        from tools.topo_schemas import load_schemas
        schemas = load_schemas(home)
        raw = schemas.get("command_rules")
    except Exception as exc:
        logger.debug("action_classifier: schemas.yaml command_rules 读取失败: %s", exc)
        return None
    if not isinstance(raw, list) or not raw:
        return None
    valid = _valid_actions(home)
    rules: List[Tuple[str, str, str, str]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            logger.warning("action_classifier: command_rules[%d] 非对象，忽略", i)
            continue
        pattern = str(item.get("pattern") or "").strip()
        action = str(item.get("action") or "").strip().lower()
        rule_id = str(item.get("rule") or f"config.{i}").strip()
        note = str(item.get("note") or "").strip()
        if not pattern or not action:
            logger.warning("action_classifier: command_rules[%d] 缺 pattern/action，忽略", i)
            continue
        if action != ACTION_UNKNOWN and valid is not None and action not in valid:
            logger.warning(
                "action_classifier: command_rules[%d] 动作 %r 不在词表，忽略", i, action,
            )
            continue
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            logger.warning("action_classifier: command_rules[%d] 正则非法（%s），忽略", i, exc)
            continue
        rules.append((pattern, action, rule_id, note))
    return rules or None


def compiled_rules(home: Optional[str] = None) -> List[Tuple[re.Pattern, str, str, str]]:
    """内置/配置规则编译表（按 schemas.yaml mtime 缓存，配置改动热生效）。"""
    try:
        from tools.topo_schemas import schemas_path
        path = schemas_path(home)
        mtime = path.stat().st_mtime
    except Exception:
        path = None
        mtime = 0.0
    key = (str(path), mtime)
    cached = _rules_cache.get(key)
    if cached is not None:
        return cached
    rules = _load_configured_rules(home) or _DEFAULT_RULES
    compiled = [
        (re.compile(p, re.IGNORECASE), action, rule_id, note)
        for p, action, rule_id, note in rules
    ]
    _rules_cache[key] = compiled
    return compiled


def _strip_sudo_prefix(command: str) -> str:
    """剥离 sudo/doas 前缀（含 -i / -u user / -E 等选项），返回命令起点。"""
    s = command.strip()
    while True:
        m = _SUDO_TOKEN_RE.match(s)
        if not m:
            return s
        rest = s[m.end():].strip()
        while True:
            fm = re.match(r"^(-[A-Za-z])(?:\s+)?", rest)
            if not fm:
                break
            flag = fm.group(1)[1:]
            rest = rest[fm.end():].strip()
            if flag in _SUDO_VALUE_FLAGS:
                val = _SUDO_SHELL_WORD_RE.match(rest)
                if val:
                    rest = rest[val.end():].strip()
        s = rest


def _main_command(segment: str) -> str:
    """段内取主命令：管道取第一段（| grep 等只影响成功标准不影响动作分类）。"""
    return _PIPE_SPLIT_RE.split(segment, maxsplit=1)[0].strip()


def classify_single(command: str) -> Dict[str, Any]:
    """单条命令（无链式操作符）→ {action, rule, note}；识别不出 → unknown。"""
    s = _strip_sudo_prefix(_main_command(command))
    if not s:
        return {"action": ACTION_UNKNOWN, "rule": None, "note": _UNKNOWN_NOTE}
    lowered = s.lower()
    for pattern, action, rule_id, note in compiled_rules():
        if pattern.search(lowered):
            return {"action": action, "rule": rule_id, "note": note}
    return {"action": ACTION_UNKNOWN, "rule": None, "note": _UNKNOWN_NOTE}


def classify_command(command: str) -> Dict[str, Any]:
    """terminal 命令串 → {action, rule, note, chain}。

    - 单条命令：action/rule/note = 命中规则的分类；识别不出 → unknown。
    - 链式（&& / || / ; / 换行）：chain = 各子命令分类列表；action = 静态档位
      最保守的候选（展示用）；矩阵层按 chain + 矩阵档位取最高（保守）。
    - 管道不干扰：``docker ps | grep harbor`` → query（主命令分类）。
    """
    if not isinstance(command, str) or not command.strip():
        return {"action": ACTION_UNKNOWN, "rule": None, "note": _UNKNOWN_NOTE, "chain": None}
    segments = _split_chain(command)
    if len(segments) <= 1:
        result = classify_single(command)
        result["chain"] = None
        return result
    chain = [classify_single(seg) for seg in segments]
    primary = max(chain, key=lambda c: _ACTION_RANK.get(c.get("action"), 11))
    return {
        "action": primary.get("action", ACTION_UNKNOWN),
        "rule": primary.get("rule"),
        "note": primary.get("note"),
        "chain": chain,
    }


def _split_chain(command: str) -> List[str]:
    parts = [p.strip() for p in _CHAIN_SPLIT_RE.split(command) if p.strip()]
    return parts
