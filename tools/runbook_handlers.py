"""YAPL P4 runbook 命令生成器（yapl-design.md §10.2 动作族契约）。

命令由执行器生成——LLM 永不接触命令语法（v0.2 runbook 无 commands，handler
生成）。本模块是唯一命令来源：``action × target 类型 × managed_by → 确定性
命令``。未覆盖组合 raise :class:`UnsupportedCommand`（报错引导，不猜命令）。

安全约定：
- 参数一律 :func:`shlex.quote` 后再进 shell 形态命令（防注入）；
- ``argv`` 形态零 shell（kubectl patch / curl 等直接参数传递）；
- 生成命令不含凭据——远端经 ssh argv 通道注入（见 ``tools/runbook_exec``）；
- ``shell=True`` 仅用于管道/``&&``/``||`` 等确定性聚合（bash -c），由执行器
  兜底超时/脱敏，与"LLM 不写命令"铁律无关（本模块是代码，不是 LLM）。

命令表示（CommandSpec）：:

    {"argv": [str,...]}                      # 零 shell 直接执行
    {"cmd": str, "shell": bool, "sudo": bool}  # shell=False → shlex.split；
                                               #   True → bash -c
    {"script_asset": <name>, "args": [...]}  # run_script 资产（执行器解析路径）
    {"transfer": {...}}                      # transfer_file（执行器走 ssh/scp）
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Dict, List, Optional

# 生命周期动词映射（enable/disable 只在支持的通道上有语义）。
_LIFECYCLE_VERBS = {
    "start": "start",
    "stop": "stop",
    "restart": "restart",
    "reload": "reload",
    "enable": "enable",
    "disable": "disable",
}
# docker/docker_compose 无 enable/disable 概念——映射到 start/stop。
_DOCKER_LIFECYCLE = {
    "start": "start",
    "stop": "stop",
    "restart": "restart",
    "reload": "restart",
    "enable": "start",
    "disable": "stop",
}


class UnsupportedCommand(ValueError):
    """未覆盖的 action × target 类型 × managed_by 组合——报错引导（不猜命令）。"""


def _q(value: Any) -> str:
    return shlex.quote(str(value))


def _kube_ns(target: Dict[str, Any]) -> str:
    ns = str(target.get("namespace") or "").strip()
    return f"-n {_q(ns)} " if ns else ""


def _backup_src(target: Dict[str, Any], params: Dict[str, Any]) -> str:
    """backup/restore 的容器内源目录推导：params.src → 实体 config/data 目录 →
    常见路径兜底。确定性、可审计（desc 里带上实际 src）。"""
    src = params.get("src")
    if isinstance(src, str) and src.strip():
        return src.strip()
    cfg = str(target.get("config_dir") or "").strip()
    if cfg:
        return cfg
    data = str(target.get("data_dir") or "").strip()
    if data:
        return data
    name = str(target.get("name") or "target")
    etype = str(target.get("type") or "")
    return f"/etc/{name}" if etype in ("gateway", "web") else f"/var/lib/{name}"


def _nginx_apply_change(container: str, change: Dict[str, Any]) -> CommandSpec:
    """nginx 配置行翻转（apply_config 的 nginx 通道）：sed -E 锚定 key 段，
    value 替换，保持缩进。key 取最后一段（http.server_tokens → server_tokens），
    兼容嵌套块内同名单行。config_file 可经 change.config_file 指定。"""
    key = str(change.get("key") or "").strip()
    if not key:
        raise UnsupportedCommand("apply_config 的 changes[].key 必填")
    seg = key.split(".")[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", seg):
        raise UnsupportedCommand(
            f"apply_config 的 key {key!r} 最后一段 {seg!r} 含非法字符——nginx 通道"
            "只支持字母/数字/下划线/连字符的配置项名"
        )
    value = str(change.get("value") or "")
    if re.search(r"[\s;'\"]", value):
        raise UnsupportedCommand(
            f"apply_config 的 value {value!r} 含空白/引号/分号——nginx 通道只支持"
            "简单标量值（如 off / on / 1m）；复杂变更请用 run_script 资产"
        )
    config_file = str(change.get("config_file") or "/etc/nginx/nginx.conf")
    script = f"s/^(\\s*{seg}\\s+).*(;)/\\1{value};/"
    return {
        "cmd": f"docker exec {_q(container)} sed -i -E {_q(script)} {_q(config_file)}",
        "shell": False,
        "sudo": False,
    }


def _kube_configmap_patch(name: str, change: Dict[str, Any],
                          target: Dict[str, Any]) -> CommandSpec:
    """kubectl configmap patch（apply_config 的 k8s 通道）。"""
    key = str(change.get("key") or "").strip()
    value = str(change.get("value") or "")
    if not key:
        raise UnsupportedCommand("apply_config 的 changes[].key 必填")
    import json as _json
    patch = _json.dumps({"data": {key: value}}, ensure_ascii=False)
    argv = ["kubectl"]
    ns = str(target.get("namespace") or "").strip()
    if ns:
        argv += ["-n", ns]
    argv += ["patch", "configmap", str(name), "--type", "merge", "-p", patch]
    return {"argv": argv, "desc": f"kubectl patch configmap {name} data.{key}"}


CommandSpec = Dict[str, Any]


def generate_commands(action: str, params: Dict[str, Any],
                      target: Dict[str, Any]) -> List[CommandSpec]:
    """动作 → 命令列表（复合动作可能多条；按序执行）。

    Args:
        action: 动作枚举（23 个之一，schemas.yaml 词表）。
        params: 步骤 params（已做变量替换 + 类型校验）。
        target: 已解析目标实体（见 runbook_exec.resolve_target 的返回结构）。

    Raises:
        UnsupportedCommand: 未覆盖组合（报错引导，不猜命令）。
    """
    name = str(target.get("name") or "")
    etype = str(target.get("type") or "")
    mb = str(target.get("managed_by") or "bare")

    if action in _LIFECYCLE_VERBS:
        return [_lifecycle(action, params, target, name, etype, mb)]

    if action in ("reboot", "shutdown"):
        return [_reboot_shutdown(action, target, name)]

    if action == "deploy":
        return _deploy(params, target, name, mb)

    if action == "rollback":
        return _rollback(params, target, name, mb)

    if action == "scale":
        return _scale(params, target, name, mb)

    if action == "decommission":
        return _decommission(params, target, name, mb)

    if action == "backup":
        return _backup(params, target, name, mb, etype)

    if action == "restore":
        return _restore(params, target, name, mb, etype)

    if action == "apply_config":
        return _apply_config(params, target, name, mb)

    if action == "query":
        return [_query(params, target, name, mb)]

    if action == "fetch_log":
        return [_fetch_log(params, target, name, mb)]

    if action == "verify":
        return [_verify(params, target, name, mb)]

    if action == "transfer_file":
        return [{"transfer": {"source": params.get("source"),
                              "dest": params.get("dest")}}]

    if action == "run_script":
        args = params.get("args") or []
        if isinstance(args, str):
            args = [args]
        args = [str(a) for a in args]
        return [{"script_asset": str(params["script"]).strip(), "args": args,
                 "desc": f"run_script 资产 {params['script']}"}]

    if action in ("install", "upgrade", "remove"):
        return [_package(action, params, target)]

    raise UnsupportedCommand(
        f"动作 {action!r} 未实现命令生成——请检查 schemas.yaml 词表与 "
        "tools/runbook_handlers.py 的 dispatch"
    )


def _container_for(target: Dict[str, Any]) -> str:
    return str(target.get("container") or target.get("name") or "")


def _compose_project(target: Dict[str, Any]) -> str:
    return str(target.get("compose_project") or "").strip()


def _compose_prefix(project: str) -> str:
    return f"-p {_q(project)} " if project else ""


def _lifecycle(action: str, params: Dict[str, Any], target: Dict[str, Any],
               name: str, etype: str, mb: str) -> CommandSpec:
    if mb == "systemd":
        svc = str(params.get("service") or name)
        return {"cmd": f"systemctl {_LIFECYCLE_VERBS[action]} {_q(svc)}",
                "shell": False, "sudo": True,
                "desc": f"systemctl {_LIFECYCLE_VERBS[action]} {svc}"}
    if mb == "docker":
        container = _container_for(target)
        if not container:
            raise UnsupportedCommand(f"{action} 目标 {name!r} 缺 docker 容器名——"
                                     "拓扑实体需声明 attrs.container / snapshot 容器名")
        return {"cmd": f"docker {_DOCKER_LIFECYCLE[action]} {_q(container)}",
                "shell": False, "sudo": False,
                "desc": f"docker {_DOCKER_LIFECYCLE[action]} {container}"}
    if mb == "docker_compose":
        project = _compose_project(target)
        svc = str(target.get("compose_service") or name)
        return {"cmd": f"docker compose {_compose_prefix(project)}"
                       f"{_DOCKER_LIFECYCLE[action]} {_q(svc)}",
                "shell": False, "sudo": False,
                "desc": f"docker compose {_compose_prefix(project).strip()} "
                        f"{_DOCKER_LIFECYCLE[action]} {svc}"}
    if mb == "kubectl":
        if action == "restart" or action == "reload":
            return {"cmd": f"kubectl {_kube_ns(target)}rollout restart "
                           f"deployment/{_q(name)}",
                    "shell": False, "sudo": False,
                    "desc": f"kubectl rollout restart deployment/{name}"}
        replicas = "1" if action in ("start", "enable") else "0"
        return {"cmd": f"kubectl {_kube_ns(target)}scale deployment/{_q(name)} "
                       f"--replicas={replicas}",
                "shell": False, "sudo": False,
                "desc": f"kubectl scale deployment/{name} --replicas={replicas}"}
    if mb == "pm2":
        verb = {k: v for k, v in _LIFECYCLE_VERBS.items()
                if k in ("start", "stop", "restart", "reload")}[action]
        return {"cmd": f"pm2 {verb} {_q(name)}", "shell": False, "sudo": False,
                "desc": f"pm2 {verb} {name}"}
    raise UnsupportedCommand(
        f"{action} 目标 {name!r}（type={etype}, managed_by={mb}）未覆盖——"
        "生命周期动作支持 systemd/docker/docker_compose/kubectl/pm2 通道"
    )


def _reboot_shutdown(action: str, target: Dict[str, Any], name: str) -> CommandSpec:
    verb = "reboot" if action == "reboot" else "poweroff"
    return {"cmd": f"systemctl {verb}", "shell": False, "sudo": True,
            "desc": f"{action} 主机 {name}"}


def _deploy(params: Dict[str, Any], target: Dict[str, Any],
            name: str, mb: str) -> List[CommandSpec]:
    image = str(params.get("image") or "").strip()
    version = str(params.get("version") or "").strip()
    if mb == "kubectl":
        cmds: List[CommandSpec] = []
        if image:
            tag = f":{version}" if version else ""
            container = str(target.get("container") or name)
            cmds.append({
                "cmd": f"kubectl {_kube_ns(target)}set image "
                       f"deployment/{_q(name)} {_q(container)}={_q(image + tag)}",
                "shell": False, "sudo": False,
                "desc": f"kubectl set image deployment/{name} {container}={image}{tag}",
            })
        else:
            cmds.append({
                "cmd": f"kubectl {_kube_ns(target)}rollout restart "
                       f"deployment/{_q(name)}",
                "shell": False, "sudo": False,
                "desc": f"kubectl rollout restart deployment/{name}",
            })
        cmds.append({
            "cmd": f"kubectl {_kube_ns(target)}rollout status "
                   f"deployment/{_q(name)} --timeout=120s",
            "shell": False, "sudo": False,
            "desc": f"kubectl rollout status deployment/{name}",
        })
        return cmds
    if mb == "docker_compose":
        project = _compose_project(target)
        svc = str(target.get("compose_service") or name)
        cmds: List[CommandSpec] = []
        if image:
            cmds.append({
                "cmd": f"docker compose {_compose_prefix(project)}pull {_q(svc)}",
                "shell": False, "sudo": False,
                "desc": f"docker compose pull {svc}",
            })
        cmds.append({
            "cmd": f"docker compose {_compose_prefix(project)}up -d "
                   f"--force-recreate {_q(svc)}",
            "shell": False, "sudo": False,
            "desc": f"docker compose up -d --force-recreate {svc}",
        })
        return cmds
    raise UnsupportedCommand(
        f"deploy 目标 {name!r}（managed_by={mb}）未覆盖——发布只支持 "
        "kubectl（set image / rollout restart）与 docker_compose（up -d）通道；"
        "单容器 docker 发布请先纳入 compose 或改用 run_script 资产"
    )


def _rollback(params: Dict[str, Any], target: Dict[str, Any],
              name: str, mb: str) -> List[CommandSpec]:
    to = str(params.get("to") or "").strip()
    if mb == "kubectl":
        suffix = f" --to-revision={_q(to)}" if to else ""
        if bool(params.get("force_undo")):
            # force_undo: true（人工确认后）→ 跳过健康检查直接 undo。
            return [{
                "cmd": f"kubectl {_kube_ns(target)}rollout undo "
                       f"deployment/{_q(name)}{suffix}",
                "shell": False, "sudo": False,
                "desc": f"kubectl rollout undo deployment/{name}"
                        + (f" --to-revision={to}" if to else ""),
            }]
        # batch79（OPS-DELTA #94）：有条件 undo——rollback 语义是"回到已知良好
        # 状态"，不是"回滚到上一个 revision"。修复步骤（prepare/scale/set
        # resources）已产生新 revision 时，undo 会把修复冲掉（2026-08-26 实测：
        # 补好 resources 的 template 被 undo 回滚到空 resources 版本 →
        # Gatekeeper 拒 → 死循环）。先 rollout status 短超时健康检查：当前
        # 健康 → 跳过 undo（记录"当前健康无需回滚"）；不健康才 undo。
        # force_undo: true 跳过检查（人工确认后）。审批门不削弱——undo 仍走
        # 矩阵 + 回滚步骤 force_confirmation。
        return [{
            "cmd": (
                f"kubectl {_kube_ns(target)}rollout status deployment/"
                f"{_q(name)} --timeout=5s && echo '当前健康无需回滚' || "
                f"kubectl {_kube_ns(target)}rollout undo "
                f"deployment/{_q(name)}{suffix}"
            ),
            "shell": True, "sudo": False,
            "desc": f"rollback {name}（先 rollout status 健康检查，健康跳过 undo）",
        }]
    if mb == "docker_compose":
        project = _compose_project(target)
        svc = str(target.get("compose_service") or name)
        return [{
            "cmd": f"docker compose {_compose_prefix(project)}up -d "
                   f"--force-recreate {_q(svc)}",
            "shell": False, "sudo": False,
            "desc": f"docker compose up -d --force-recreate {svc}（按 compose "
                    "配置回滚到上一版本镜像；精确版本回滚用 run_script 资产）",
        }]
    raise UnsupportedCommand(
        f"rollback 目标 {name!r}（managed_by={mb}）未覆盖——回滚只支持 "
        "kubectl（rollout undo）与 docker_compose（up -d --force-recreate）通道"
    )


def _scale(params: Dict[str, Any], target: Dict[str, Any],
           name: str, mb: str) -> List[CommandSpec]:
    replicas = int(params.get("replicas") or 1)
    if replicas < 0:
        raise UnsupportedCommand(f"scale.replicas 必须 ≥ 0，收到 {replicas}")
    if mb == "kubectl":
        return [{
            "cmd": f"kubectl {_kube_ns(target)}scale deployment/{_q(name)} "
                   f"--replicas={replicas}",
            "shell": False, "sudo": False,
            "desc": f"kubectl scale deployment/{name} --replicas={replicas}",
        }]
    if mb == "docker_compose":
        project = _compose_project(target)
        svc = str(target.get("compose_service") or name)
        return [{
            "cmd": f"docker compose {_compose_prefix(project)}up -d "
                   f"--scale {_q(svc)}={replicas}",
            "shell": False, "sudo": False,
            "desc": f"docker compose up -d --scale {svc}={replicas}",
        }]
    if mb == "pm2":
        return [{"cmd": f"pm2 scale {_q(name)} {replicas}",
                 "shell": False, "sudo": False,
                 "desc": f"pm2 scale {name} {replicas}"}]
    raise UnsupportedCommand(
        f"scale 目标 {name!r}（managed_by={mb}）未覆盖——伸缩只支持 "
        "kubectl / docker_compose / pm2 通道"
    )


def _decommission(params: Dict[str, Any], target: Dict[str, Any],
                  name: str, mb: str) -> List[CommandSpec]:
    if mb == "kubectl":
        return [{"cmd": f"kubectl {_kube_ns(target)}delete "
                        f"deployment/{_q(name)}",
                 "shell": False, "sudo": False,
                 "desc": f"kubectl delete deployment/{name}"}]
    if mb == "docker_compose":
        project = _compose_project(target)
        svc = str(target.get("compose_service") or name)
        return [{"cmd": f"docker compose {_compose_prefix(project)}rm -sf {_q(svc)}",
                 "shell": False, "sudo": False,
                 "desc": f"docker compose rm -sf {svc}"}]
    if mb == "docker":
        container = _container_for(target)
        if not container:
            raise UnsupportedCommand(f"decommission 目标 {name!r} 缺 docker 容器名")
        return [{"cmd": f"docker rm -f {_q(container)}", "shell": False,
                 "sudo": False, "desc": f"docker rm -f {container}"}]
    if mb == "systemd":
        svc = str(params.get("service") or name)
        return [{"cmd": f"systemctl disable --now {_q(svc)}", "shell": False,
                 "sudo": True, "desc": f"systemctl disable --now {svc}"}]
    raise UnsupportedCommand(
        f"decommission 目标 {name!r}（managed_by={mb}）未覆盖——退役只支持 "
        "kubectl / docker_compose / docker / systemd 通道"
    )


def _backup(params: Dict[str, Any], target: Dict[str, Any], name: str,
            mb: str, etype: str) -> List[CommandSpec]:
    dest = str(params.get("dest") or "").strip()
    if not dest:
        raise UnsupportedCommand(f"backup 目标 {name!r} 的 dest 必填")
    if mb in ("docker", "docker_compose"):
        container = _container_for(target)
        if not container:
            raise UnsupportedCommand(f"backup 目标 {name!r} 缺 docker 容器名")
        src = _backup_src(target, params)
        parent = dest.rsplit("/", 1)[0] if "/" in dest else ""
        if parent:
            # docker cp 要求目标父目录存在——先 mkdir -p（本地主机侧）。
            return [{
                "cmd": (f"mkdir -p {_q(parent)} && "
                        f"docker cp {_q(container)}:{_q(src)} {_q(dest)}"),
                "shell": True, "sudo": False,
                "desc": f"docker cp {container}:{src} {dest}",
            }]
        return [{"cmd": f"docker cp {_q(container)}:{_q(src)} {_q(dest)}",
                 "shell": False, "sudo": False,
                 "desc": f"docker cp {container}:{src} {dest}"}]
    if etype == "host" or mb == "bare":
        src = str(params.get("src") or "").strip()
        if not src:
            raise UnsupportedCommand(
                f"backup 主机目标 {name!r} 需要 src 参数（备份哪个路径），"
                "例如 src=/etc/nginx"
            )
        parent = dest.rsplit("/", 1)[0] if "/" in dest else ""
        if parent:
            return [{
                "cmd": (f"mkdir -p {_q(parent)} && "
                        f"tar -czf {_q(dest)} {_q(src)}"),
                "shell": True, "sudo": True,
                "desc": f"tar -czf {dest} {src}",
            }]
        return [{"cmd": f"tar -czf {_q(dest)} {_q(src)}", "shell": False,
                 "sudo": True, "desc": f"tar -czf {dest} {src}"}]
    raise UnsupportedCommand(
        f"backup 目标 {name!r}（type={etype}, managed_by={mb}）未覆盖——"
        "备份支持 docker 容器（docker cp）与主机（tar）通道"
    )


def _restore(params: Dict[str, Any], target: Dict[str, Any], name: str,
             mb: str, etype: str) -> List[CommandSpec]:
    src = str(params.get("from") or "").strip()
    if not src:
        raise UnsupportedCommand(f"restore 目标 {name!r} 的 from 必填")
    if mb in ("docker", "docker_compose"):
        container = _container_for(target)
        if not container:
            raise UnsupportedCommand(f"restore 目标 {name!r} 缺 docker 容器名")
        dst = _backup_src(target, params)
        # 目录复原语义：src 是 backup 产物目录 → 内容拷入 dst（拖尾 /. 与 /，
        # 避免 docker cp 把 src 目录塞成 dst/src 子目录）。
        return [{"cmd": f"docker cp {_q(src)}/. {_q(container)}:{_q(dst)}/",
                 "shell": False, "sudo": False,
                 "desc": f"docker cp {src}/. {container}:{dst}/"}]
    if etype == "host" or mb == "bare":
        dst = str(params.get("dest") or "").strip() or _backup_src(target, params)
        return [{"cmd": f"tar -xzf {_q(src)} -C {_q(dst)}", "shell": False,
                 "sudo": True, "desc": f"tar -xzf {src} -C {dst}"}]
    raise UnsupportedCommand(
        f"restore 目标 {name!r}（type={etype}, managed_by={mb}）未覆盖——"
        "恢复支持 docker 容器（docker cp）与主机（tar）通道"
    )


def _apply_config(params: Dict[str, Any], target: Dict[str, Any], name: str,
                  mb: str) -> List[CommandSpec]:
    changes = params.get("changes") or []
    name_lower = str(name).lower()
    if mb in ("docker", "docker_compose") and name_lower in ("nginx", "nginx-web"):
        container = _container_for(target)
        if not container:
            raise UnsupportedCommand(f"apply_config 目标 {name!r} 缺 docker 容器名")
        return [_nginx_apply_change(container, ch) for ch in changes]
    if mb == "kubectl":
        return [_kube_configmap_patch(name, ch, target) for ch in changes]
    raise UnsupportedCommand(
        f"apply_config 目标 {name!r}（managed_by={mb}）未覆盖——配置下发支持 "
        "nginx 容器（sed 行翻转，key 取末段）与 kubectl configmap（patch data）通道；"
        "其他服务配置变更请用 run_script 资产（只引用，不内联）"
    )


def _query(params: Dict[str, Any], target: Dict[str, Any], name: str,
           mb: str) -> CommandSpec:
    pattern = str(params.get("pattern") or "").strip()
    has_target = bool(target)
    base_shell = False
    if has_target and mb == "docker" or has_target and mb == "docker_compose":
        container = _container_for(target)
        base = (f"docker ps --filter name={_q(container)} "
                f"--format '{{{{.Names}}}}\\t{{{{.Status}}}}'")
    elif has_target and mb == "systemd":
        base = f"systemctl status {_q(str(params.get('service') or name))} --no-pager"
    elif has_target and mb == "kubectl":
        base = f"kubectl {_kube_ns(target)}get pods -l app={_q(name)}"
    elif has_target and mb == "pm2":
        base = f"pm2 describe {_q(name)}"
    elif has_target:
        # 含管道 + || true 的确定性聚合 → 必须 shell 执行（bash -c），否则
        # shlex.split 把管道当参数（ps aux | grep → garbage option）。
        base = f"ps aux | grep {_q(name)} || true"
        base_shell = True
    elif pattern:
        base = "ps aux"
        base_shell = True
    else:
        return {"cmd": "uname -a; uptime; df -h /", "shell": True, "sudo": False,
                "desc": "主机基础查询（内核/负载/磁盘）"}
    if pattern:
        return {"cmd": f"{base} | grep {_q(pattern)} || true", "shell": True,
                "sudo": False, "desc": f"{base} | grep {pattern}"}
    return {"cmd": base, "shell": base_shell, "sudo": False, "desc": base}


def _fetch_log(params: Dict[str, Any], target: Dict[str, Any], name: str,
               mb: str) -> CommandSpec:
    lines = int(params.get("lines") or 100)
    if lines <= 0:
        lines = 100
    grep = str(params.get("grep") or "").strip()
    since = str(params.get("since") or "").strip()
    if mb in ("docker", "docker_compose"):
        container = _container_for(target)
        if not container:
            raise UnsupportedCommand(f"fetch_log 目标 {name!r} 缺 docker 容器名")
        base = f"docker logs --tail {lines} {_q(container)}"
        if since:
            base += f" --since {_q(since)}"
    elif mb == "systemd":
        svc = str(params.get("service") or name)
        base = f"journalctl -u {_q(svc)} -n {lines} --no-pager"
        if since:
            base += f" --since {_q(since)}"
    elif mb == "kubectl":
        base = f"kubectl {_kube_ns(target)}logs deployment/{_q(name)} --tail={lines}"
    elif mb == "pm2":
        base = f"pm2 logs {_q(name)} --lines {lines} --nostream"
    else:
        raise UnsupportedCommand(
            f"fetch_log 目标 {name!r}（managed_by={mb}）未覆盖——日志支持 "
            "docker/docker_compose/systemd/kubectl/pm2 通道"
        )
    if grep:
        return {"cmd": f"{base} | grep {_q(grep)} || true", "shell": True,
                "sudo": False, "desc": f"{base} | grep {grep}"}
    return {"cmd": base, "shell": False, "sudo": False, "desc": base}


def _verify(params: Dict[str, Any], target: Dict[str, Any], name: str,
            mb: str) -> CommandSpec:
    if mb in ("docker", "docker_compose"):
        container = _container_for(target)
        if not container:
            raise UnsupportedCommand(f"verify 目标 {name!r} 缺 docker 容器名")
        return {"cmd": f"docker inspect -f '{{{{.State.Status}}}}' {_q(container)}",
                "shell": False, "sudo": False,
                "desc": f"docker inspect {container} 状态"}
    if mb == "systemd":
        svc = str(params.get("service") or name)
        return {"cmd": f"systemctl is-active {_q(svc)}", "shell": False,
                "sudo": False, "desc": f"systemctl is-active {svc}"}
    if mb == "kubectl":
        return {"cmd": f"kubectl {_kube_ns(target)}rollout status "
                       f"deployment/{_q(name)} --timeout=60s",
                "shell": False, "sudo": False,
                "desc": f"kubectl rollout status deployment/{name}"}
    if mb == "pm2":
        return {"cmd": f"pm2 describe {_q(name)}", "shell": False, "sudo": False,
                "desc": f"pm2 describe {name}"}
    raise UnsupportedCommand(
        f"verify 目标 {name!r}（managed_by={mb}）未覆盖——验证支持 "
        "docker/docker_compose/systemd/kubectl/pm2 通道"
    )


def _package(action: str, params: Dict[str, Any],
             target: Dict[str, Any]) -> CommandSpec:
    package = str(params.get("package") or "").strip()
    if not package:
        raise UnsupportedCommand(f"{action} 的 package 必填")
    version = str(params.get("version") or "").strip()
    repo = str(params.get("repo") or "").strip()
    os_name = str(target.get("os") or "").lower()
    if any(k in os_name for k in ("ubuntu", "debian")):
        pkg_mgr, ver_sep = "apt-get", "="
    elif any(k in os_name for k in ("rocky", "centos", "rhel", "fedora",
                                    "alma", "oracle")):
        pkg_mgr, ver_sep = "dnf", "-"
    else:
        raise UnsupportedCommand(
            f"{action} 目标 {target.get('name') or ''!r} 的 os={os_name!r} 未覆盖"
            "——包管理只支持 apt（Ubuntu/Debian）与 dnf（Rocky/CentOS/RHEL）"
        )
    if action == "remove":
        deps = bool(params.get("deps"))
        flag = " --auto-remove" if (deps and pkg_mgr == "apt-get") else ""
        cmd = f"{pkg_mgr} remove -y {_q(package)}{flag}"
    else:
        verb = "install" if action == "install" else "upgrade"
        ver = f"{ver_sep}{_q(version)}" if version else ""
        repo_flag = f" -t {_q(repo)}" if (repo and pkg_mgr == "apt-get") else ""
        repo_flag = (f" --enablerepo={_q(repo)}"
                     if (repo and pkg_mgr == "dnf") else repo_flag)
        cmd = f"{pkg_mgr} {verb} -y {_q(package)}{ver}{repo_flag}"
    return {"cmd": cmd, "shell": False, "sudo": True,
            "desc": f"{pkg_mgr} {action} {package}"
                    + (f"={version}" if version else "")}


def generate_expect_check(expect: Dict[str, Any],
                          target: Dict[str, Any]) -> List[CommandSpec]:
    """expect → 检查命令（§10.3 检查通道确定性生成）。

    Returns 检查命令列表（可能多条——contains 每对象一条）。调用方跑完后用
    :func:`evaluate_expect` 断言。
    """
    channel = str(expect.get("target") or "")
    checks: List[CommandSpec] = []
    if channel == "http":
        url = str(expect.get("url") or "").strip()
        if not url:
            raise UnsupportedCommand("expect.target=http 需要 url")
        if "body_contains" in expect:
            checks.append({"cmd": f"curl -sS -L {_q(url)}", "shell": False,
                           "sudo": False, "desc": f"curl {url}"})
        else:
            checks.append({"cmd": f"curl -sS -L -o /dev/null -w '%{{http_code}}' "
                                  f"{_q(url)}",
                           "shell": False, "sudo": False,
                           "desc": f"curl -w http_code {url}"})
        return checks
    if channel in ("docker", "docker_compose"):
        container = str(expect.get("object") or _container_for(target) or "")
        checks.append({"cmd": f"docker inspect -f '{{{{json .State}}}}' "
                              f"{_q(container)}",
                       "shell": False, "sudo": False,
                       "desc": f"docker inspect {container} state"})
        return checks
    if channel == "systemctl":
        obj = str(expect.get("object") or target.get("name") or "")
        checks.append({"cmd": f"systemctl status {_q(obj)} --no-pager",
                       "shell": False, "sudo": False,
                       "desc": f"systemctl status {obj}"})
        return checks
    if channel == "kubectl":
        obj = str(expect.get("object") or target.get("name") or "")
        kind = str(expect.get("kind") or "pod")
        if kind == "pod":
            # 默认查 pod（batch78，OPS-DELTA #93）：pod 名带随机后缀
            # （argocd-server-86678dcc97-n5cfx），不能 get pod/<name>——按
            # label 查；"1/1 Running" 是 pod 状态语义（READY/RUNNING 两列），
            # deployment 的 READY 列是 "1/1 1 1"（ready/up-to-date/available），
            # 语义不同。batch79（OPS-DELTA #94）：label 不硬编码 app=——k8s
            # 推荐 label（app.kubernetes.io/name）应用（argocd 等）没有 app=
            # label，硬编码查询返回空 → expect 必败（2026-08-26 node1 实测）。
            # pod 通道先读 deployment 的真实 selector（.spec.selector.
            # matchLabels）再按真实 label 查 pod；selector 为空回退 app=<name>；
            # 读取失败 fail-closed。expect.selector 显式覆盖（跳过读取），
            # expect.label 兼容旧行为（app=<name>）。
            selector = str(expect.get("selector") or "").strip()
            if selector:
                checks.append({"cmd": f"kubectl {_kube_ns(target)}get pods "
                                      f"-l {selector} -o wide",
                               "shell": False, "sudo": False,
                               "desc": f"kubectl get pods -l {selector}"})
                return checks
            label = str(expect.get("label") or "").strip()
            if label:
                checks.append({"cmd": f"kubectl {_kube_ns(target)}get pods "
                                      f"-l app={_q(label)} -o wide",
                               "shell": False, "sudo": False,
                               "desc": f"kubectl get pods -l app={label}"})
                return checks
            ns = _kube_ns(target)
            # 两步合并成一条 bash 命令：selector 读取是中间产物，不进 expect
            # body（expect 只取最终 stdout 的 pod 列表）。
            checks.append({
                "cmd": (
                    f"S=$(kubectl {ns}get deployment/{_q(obj)} "
                    f"-o jsonpath='{{.spec.selector.matchLabels}}') || "
                    f"{{ echo \"无法读取 deployment {obj} selector\" >&2; exit 1; }}; "
                    f"[ -n \"$S\" ] || S={_q(f'app={obj}')}; "
                    f"if [ \"$S\" != {_q(f'app={obj}')} ]; then "
                    f"S=$(printf '%s' \"$S\" | python3 -c 'import sys,json; "
                    f"d=json.load(sys.stdin); "
                    f"print(\" \".join(f\"{{k}}={{v}}\" for k,v in d.items()))'); fi; "
                    f"kubectl {ns}get pods -l \"$S\" -o wide"
                ),
                "shell": True, "sudo": False,
                "desc": f"kubectl get pods -l <{obj} 真实 selector> -o wide",
            })
            return checks
        if kind in ("deployment", "statefulset", "sts"):
            token = "sts" if kind in ("statefulset", "sts") else "deployment"
            checks.append({"cmd": f"kubectl {_kube_ns(target)}get "
                                  f"{token}/{_q(obj)} -o wide",
                           "shell": False, "sudo": False,
                           "desc": f"kubectl get {token}/{obj}"})
            return checks
        raise UnsupportedCommand(
            f"expect.kind={kind!r} 未覆盖——kubectl 检查通道支持 "
            "pod/deployment/statefulset(sts)"
        )
    if channel == "pm2":
        obj = str(expect.get("object") or target.get("name") or "")
        checks.append({"cmd": f"pm2 describe {_q(obj)}", "shell": False,
                       "sudo": False, "desc": f"pm2 describe {obj}"})
        return checks
    if channel == "process":
        pat = str(expect.get("pattern") or expect.get("object") or "")
        if not pat:
            raise UnsupportedCommand("expect.target=process 需要 pattern/object")
        checks.append({"cmd": f"pgrep -af {_q(pat)} || true", "shell": True,
                       "sudo": False, "desc": f"pgrep -af {pat}"})
        return checks
    if channel == "port":
        port = str(expect.get("port") or "").strip()
        if not port:
            raise UnsupportedCommand("expect.target=port 需要 port")
        host = str(expect.get("host") or "127.0.0.1")
        checks.append({"cmd": f"bash -c 'exec 3<>/dev/tcp/{host}/{port}' "
                              f"2>/dev/null || exit 1", "shell": True,
                       "sudo": False, "desc": f"tcp 探测 {host}:{port}"})
        return checks
    raise UnsupportedCommand(
        f"expect.target={channel!r} 未覆盖——检查通道支持 "
        "http/docker/docker_compose/systemctl/kubectl/pm2/process/port"
    )


def evaluate_expect(expect: Dict[str, Any], results: List[Dict[str, Any]]) -> tuple:
    """expect 断言（§10.3）：检查命令结果 → (ok, detail)。

    Args:
        expect: 步骤 expect（{target, contains?, http_status?, body_contains?,
            exit_code?}）。
        results: 与 :func:`generate_expect_check` 输出对齐的执行结果列表
            （每项 {exit_code, stdout, stderr}）。

    Returns:
        (ok: bool, detail: str)。
    """
    if not results:
        return False, "expect 检查命令为空（生成失败）"
    first = results[0]
    if "exit_code" in expect:
        want = int(expect["exit_code"])
        if first.get("exit_code") != want:
            return False, (f"exit_code 期望 {want}，实际 {first.get('exit_code')}："
                           f"{str(first.get('stderr') or first.get('stdout') or '')[:200]}")
    if "http_status" in expect:
        want = int(expect["http_status"])
        got_raw = str(first.get("stdout") or "").strip()
        try:
            got = int(got_raw[:3])
        except ValueError:
            return False, f"http_status 期望 {want}，curl 输出无法解析: {got_raw[:60]!r}"
        if got != want:
            return False, f"http_status 期望 {want}，实际 {got}"
    if "body_contains" in expect:
        needle = str(expect["body_contains"])
        body = str(first.get("stdout") or "")
        # kubectl 列对齐是多空格（视觉格式非内容语义）——归一化空白后匹配，
        # 否则 body_contains "1/1 Running" 永远匹配不上真实输出
        # "argocd-server-...   1/1     Running   0  37s"（2026-08-25 实测）。
        norm = lambda s: " ".join(s.split())
        if norm(needle) not in norm(body):
            return False, f"body_contains 期望包含 {needle!r}，响应体未命中（{body[:200]}）"
    contains = expect.get("contains")
    if isinstance(contains, dict):
        for obj, want in contains.items():
            out = str(first.get("stdout") or "")
            if str(want) not in out:
                return False, f"contains 期望 {obj} 包含 {want!r}，输出未命中（{out[:200]}）"
    # 无显式谓词时：exit 0 = 通过（隐式成功标准）。
    if first.get("exit_code") != 0:
        return False, (f"检查命令 exit {first.get('exit_code')}："
                       f"{str(first.get('stderr') or first.get('stdout') or '')[:200]}")
    return True, "expect 全部通过"
