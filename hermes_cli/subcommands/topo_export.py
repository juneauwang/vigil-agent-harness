"""``vigil topo export`` / ``vigil topo reset`` —— 拓扑表可视化导出 + 清空入口。

把三层拓扑（topology.yaml + hosts/*.yaml + 可选 entities/*.yaml）渲染成单个
**自包含** HTML 文件（零 CDN / 零 JS 库，内联 CSS+JS，file:// 直接打开）：
集群分组 + 主机卡片（name/env/endpoint/role/runtime + 服务列表）+ 跨主机实体
区块 + 关键链路（key_paths）高亮 + 状态着色 + 搜索过滤 + 详情展开。

**凭据安全（硬要求）**：任何 credential 段（type/ref/user）与用户名、密码、
token、私钥路径等**绝不进 HTML**——``_sanitize`` 递归剔除敏感键（credential/
user/password/passphrase/secret/token/api_key/ssh_key/... 任意深度，含
attrs/ssh 段内嵌），值级再做一轮密钥路径/URL userinfo 脱敏；渲染只输出
endpoint/role/status 等非敏感字段。测试用 grep 断言 ref 路径永不出现。

**分享脱敏**：数据根显示为 ``~`` 相对路径（``~/.vigil`` 而非
``/home/<user>/.vigil``），值级同样掩码 home 前缀——HTML 分享时不泄露本地
用户名/目录结构。

约束：不碰 conversation_loop / prompt 缓存 / 压缩；不新增环境变量；只改
本文件 + main.py 注册 + 对应测试。

``vigil topo reset``（OPS-DELTA #101，batch85）：清空全部拓扑数据（topology.yaml
+ entities/ + services/ + hosts/ + hardware/，回到未初始化状态）——从 0 重建
拓扑的正规入口（rm 拓扑文件会被目标解析 deny 拦死）。破坏性操作：交互确认
（y/N）+ 审计记录；命令直接操作文件，不走 terminal 命令裁决。命名语义：与
``vigil matrix reset``（回退模板）不同——topo reset = 清空到未初始化。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# 凭据过滤
# ---------------------------------------------------------------------------

# 递归剔除的敏感键（任意深度；key_paths=关键链路是显示特性，绝不在此列）。
_SENSITIVE_KEYS = frozenset({
    "credential", "credentials",
    "password", "passphrase", "sudo_password",
    "secret", "secrets", "token", "tokens",
    "api_key", "apikey", "api-key", "access_key", "access-key",
    "private_key", "private-key",
    "ssh_key", "vault", "askpass", "key_path", "ref",
    "user",
})

# 值级脱敏：私钥路径/文件名、URL userinfo（user:pass@）。凭据段已被结构性
# 剔除，这里作为兜底（防凭据样值出现在非凭据字段里）。
_CRED_VALUE_PATTERNS = [
    re.compile(r"(?i)~?[\w./-]*\.ssh/[^\s\"'<>]+"),           # ~/.ssh/... 路径
    re.compile(r"(?i)\b[\w./~-]+\.(?:pem|ppk|key)(?:\.pub)?\b"),  # 私钥文件
    re.compile(r"(?i)\b(?:id_rsa|id_ed25519|id_ecdsa|id_dsa)\b"),  # 标准私钥名
    re.compile(r"(?i)://[^\s/@]+:[^\s/@]+@"),                  # URL user:pass@
]
_REDACTED = "[已过滤]"


def _redact_value(value: str) -> str:
    """把字符串里的凭据样值（私钥路径/文件、URL userinfo）替换为占位符。"""
    out = value
    for pattern in _CRED_VALUE_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    # 值级兜底：绝对 home 前缀 → ~（防实体数据里出现 /home/<user>/...，
    # 分享场景的轻度泄露；带边界，/home/u2 不会被误替换）。
    try:
        home = str(Path.home())
    except Exception:
        home = ""
    if home and home != "/":
        out = re.sub(re.escape(home) + r"(?=/|$)", "~", out)
    return out


def _display_path(path: Path) -> str:
    """把路径显示为 ``~`` 相对（脱敏用户名；分享安全）。

    数据根默认在 home 下：显示 ``~/.vigil`` 而非 ``/home/<user>/.vigil``，
    避免 HTML 分享时泄露本地用户名/目录结构。不在 home 下的路径原样显示
    （自定义 VIGIL_HOME 属于用户主动配置的位置）。
    """
    text = str(path)
    try:
        home = str(Path.home())
    except Exception:
        return text
    if not home or home == "/":
        return text
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


def _sanitize(obj: Any) -> Any:
    """递归剔除敏感键 + 值级脱敏。返回结果可安全进入 HTML。

    - dict：丢弃 _SENSITIVE_KEYS 命中的键（整个子树，含 attrs/ssh 段内嵌
      credential/user），其余递归；
    - str：值级脱敏（私钥路径 / .pem 文件 / URL userinfo）。
    """
    if isinstance(obj, dict):
        return {
            k: _sanitize(v)
            for k, v in obj.items()
            if k not in _SENSITIVE_KEYS
        }
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, str):
        return _redact_value(obj)
    return obj


def _esc(value: Any) -> str:
    """HTML 转义（防 XSS + 防格式破坏）。"""
    return html.escape(str(value), quote=True)


# ---------------------------------------------------------------------------
# 数据模型（build_view → 纯 dict，凭据已过滤，供 render_html 消费）
# ---------------------------------------------------------------------------


def _topology_host_names(home: Optional[Path] = None) -> set:
    """拓扑表 hosts 段全部主机名（批三十五：本地执行打点按名匹配用）。

    读取失败/无 hosts → 空集。仅名字集合，不触敏感字段。
    """
    try:
        from tools.topo_tools import load_topology
        from hermes_constants import get_hermes_home
        topo = load_topology(home or Path(get_hermes_home()))
        return {str(r.get("name") or "").strip() for r in (topo or {}).get("hosts") or []}
    except Exception:
        return set()


def _card_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """紧凑卡片显示字段（白名单；row 已 sanitize，凭据值不可能在此）。

    v0.4：服务行补 managed_by/extra_ports/log_paths/depends_on（图连线用）；
    role/runtime 数组原样透传（前端数组渲染）。
    """
    return {
        "name": row.get("name", ""),
        "type": row.get("type", ""),
        "env": row.get("env", ""),
        "cluster": str(row.get("cluster") or "default"),
        "endpoint": row.get("endpoint", ""),
        "role": row.get("role", ""),
        "runtime": row.get("runtime", ""),
        "status": row.get("status", ""),
        "owner": row.get("owner", ""),
        "description": row.get("description", ""),
        "port": row.get("port", ""),
        "ports": row.get("ports") or [],
        "managed_by": row.get("managed_by", ""),
        "extra_ports": row.get("extra_ports") or [],
        "log_paths": row.get("log_paths") or [],
        "depends_on": row.get("depends_on") or [],
    }


def _derive_status(statuses: List[Any]) -> str:
    """从子级状态推导聚合状态（批三十三 4）：

    全 running/健康 → ``running``；任一非健康（stopped/exited/degraded/…）→
    ``degraded``；全部空 → 空串（不显示 pill，避免误显示）。
    """
    non_empty = [str(s).strip().lower() for s in statuses if str(s or "").strip()]
    if not non_empty:
        return ""
    healthy = {"running", "up", "healthy", "ok", "active", "online"}
    if all(s in healthy for s in non_empty):
        return "running"
    return "degraded"


def _load_detail(home: Path, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """第三层档案（可选）：_load_entity_file 失败/缺失 → None；成功 → sanitize。"""
    try:
        from tools.topo_tools import _load_entity_file
        detail = _load_entity_file(home, row)
    except Exception:
        return None
    if not isinstance(detail, dict):
        return None
    # 内部 plumbing 键不渲染（services_index/detail 只是文件路径引用）。
    detail = {k: v for k, v in detail.items() if k not in ("services_index", "detail")}
    return _sanitize(detail) or None


def build_view(home: Path) -> Optional[Dict[str, Any]]:
    """读取三层拓扑 → 凭据过滤后的视图模型。无拓扑数据/空表 → None。

    结构：
      generated_at / data_root / version / sources / environments
      clusters: [{name, env, description, owner, type, provenance, endpoint}]
      hosts:    [{_card_fields + on_key_path + services[{card + detail}] + services_missing}]
      cross_host: [{_card_fields + on_key_path + detail}]（v0.4 恒空；v0.2/3 兼容）
      key_paths: [[实体名链...]]（v0.4 恒空；v0.2/3 兼容）
      key_path_entity_names: {链上实体名}
      details:  { "<kind>:<name>": {sanitized 第三层档案} }
    """
    try:
        from tools.topo_tools import load_topology, topo_first_layer
    except Exception:
        return None
    topo = load_topology(home)
    if topo is None:
        return None
    if not (topo.get("hosts") or topo.get("cross_host")
            or topo.get("clusters") or topo.get("core_entities")):
        return None

    first = topo_first_layer(topo)

    # 批三十五：host 活性（runtime_state，lazy last_seen）整表读一次，合并进
    # host card；无记录 → 不返回该字段（前端显示"未探测"）。
    try:
        from hermes_cli.runtime_state import load_activity
        activity = load_activity(home)
    except Exception:
        activity = {}

    chains: List[List[str]] = []
    for chain in topo.get("key_paths") or []:
        if isinstance(chain, list):
            chains.append([str(n) for n in chain if isinstance(n, str)])
    kp_names = {name for chain in chains for name in chain}

    view: Dict[str, Any] = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_root": _display_path(home),
        "version": topo.get("version"),
        "sources": [str(s) for s in (topo.get("sources") or []) if isinstance(s, str)],
        "environments": [str(e.get("name") or "") for e in (topo.get("environments") or [])
                         if isinstance(e, dict) and e.get("name")],
        "clusters": [],
        "hosts": [],
        "cross_host": [],
        "key_paths": chains,
        "key_path_entity_names": sorted(kp_names),
        "details": {},
    }

    if first.get("hosts") is None:
        # v0.1 兼容：扁平 core_entities（无 hosts/clusters 概念）。
        for row in first.get("core_entities") or []:
            if not isinstance(row, dict):
                continue
            card = _card_fields(_sanitize(row))
            if not card["name"]:
                continue
            card["on_key_path"] = card["name"] in kp_names
            card["kind"] = "entity"
            view["hosts"].append({"card": card, "services": [], "services_missing": True,
                                  "detail": _load_detail(home, row)})
        return view

    for row in first.get("clusters") or []:
        if not isinstance(row, dict):
            continue
        view["clusters"].append({
            "name": str(row.get("name") or ""),
            "env": str(row.get("env") or ""),
            "description": str(row.get("description") or ""),
            "owner": str(row.get("owner") or ""),
            "type": str(row.get("type") or ""),
            "provenance": str(row.get("provenance") or ""),
            "endpoint": str(row.get("endpoint") or ""),
        })
    view["clusters"] = [c for c in view["clusters"] if c["name"]]

    try:
        from tools.topo_tools import _load_host_index
    except Exception:
        _load_host_index = None  # type: ignore[assignment]

    for row in first.get("hosts") or []:
        if not isinstance(row, dict):
            continue
        card = _card_fields(_sanitize(row))
        if not card["name"]:
            continue
        card["on_key_path"] = card["name"] in kp_names
        card["kind"] = "host"
        last_seen = activity.get(card["name"])
        if last_seen:
            card["last_seen"] = last_seen
        services: List[Dict[str, Any]] = []
        services_missing = True
        index: Dict[str, Any] = {}
        if _load_host_index is not None:
            try:
                index = _load_host_index(home, row) or {}
            except Exception:
                index = {}
        # YAPL P2（OPS-DELTA #68）：v0.4 服务行不冗余 cluster——读取端从所属 host
        # 继承（host.cluster → index.cluster → default），显式 cluster 仍优先。
        host_cluster = str(row.get("cluster") or index.get("cluster") or "default")
        for svc in index.get("services") or []:
            if not isinstance(svc, dict):
                continue
            s_row = dict(svc)
            s_row.setdefault("cluster", host_cluster)
            s_card = _card_fields(_sanitize(s_row))
            if not s_card["name"]:
                continue
            s_card["on_key_path"] = s_card["name"] in kp_names
            s_card["kind"] = "service"
            detail = _load_detail(home, s_row)
            # 批三十三 4：服务状态在第三层 entities/*.yaml（status 字段），
            # 从 detail merge（detail 已 sanitize；无 status/load 失败保持空）。
            detail_status = str((detail or {}).get("status") or "").strip()
            if detail_status:
                s_card["status"] = detail_status
            services.append({
                "card": s_card,
                "detail": detail,
            })
        if services:
            services_missing = False
        # 批三十三 4：主机卡状态由服务推导（全健康 → running；任一
        # stopped/exited/degraded → degraded；全部空 → 空不显示）。
        if not str(card.get("status") or "").strip():
            derived = _derive_status([s["card"].get("status") for s in services])
            if derived:
                card["status"] = derived
        view["hosts"].append({
            "card": card,
            "services": services,
            "services_missing": services_missing,
            "detail": _load_detail(home, row),
        })

    # 批三十三 4：集群卡状态由主机推导（同规则）。
    for cluster in view["clusters"]:
        host_statuses = [
            h["card"].get("status")
            for h in view["hosts"]
            if str(h["card"].get("cluster") or "default") == str(cluster.get("name") or "default")
        ]
        derived = _derive_status(host_statuses)
        if derived:
            cluster["status"] = derived

    for row in first.get("cross_host") or []:
        if not isinstance(row, dict):
            continue
        card = _card_fields(_sanitize(row))
        if not card["name"]:
            continue
        card["on_key_path"] = card["name"] in kp_names
        card["kind"] = "cross_host"
        view["cross_host"].append({
            "card": card,
            "detail": _load_detail(home, row),
        })

    # 详情索引：<kind>:<name> → sanitized 档案（JS 展开用）。服务键带 host 前缀
    # 消歧（同 host 内唯一），与渲染侧 _service_rows 的 detail_key 严格一致。
    def _register(full_key: str, detail: Optional[Dict[str, Any]]) -> None:
        if detail:
            view["details"][full_key] = detail

    for host in view["hosts"]:
        _register(f"host:{host['card']['name']}", host["detail"])
        for svc in host["services"]:
            _register(f"service:{host['card']['name']}:{svc['card']['name']}",
                      svc["detail"])
    for cross in view["cross_host"]:
        _register(f"cross:{cross['card']['name']}", cross["detail"])

    return view


# ---------------------------------------------------------------------------
# HTML 渲染（零外部依赖，自包含）
# ---------------------------------------------------------------------------

_CSS = """\
:root { --bg:#f4f6f9; --card:#ffffff; --border:#e2e6ec; --text:#1f2430; --muted:#6b7280; --accent:#2f6fed; --kp:#b45309; }
* { box-sizing: border-box; }
body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; background:var(--bg); color:var(--text); }
header { position:sticky; top:0; z-index:10; background:#fff; border-bottom:1px solid var(--border); padding:14px 22px; box-shadow:0 1px 3px rgba(0,0,0,.04); }
header h1 { margin:0 0 6px; font-size:20px; }
.meta { color:var(--muted); font-size:12px; margin-bottom:10px; line-height:1.6; }
.controls { display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
#topo-search { flex:1; min-width:220px; padding:8px 12px; border:1px solid var(--border); border-radius:8px; font-size:14px; }
#topo-search:focus { outline:2px solid rgba(47,111,237,.25); border-color:var(--accent); }
.legend { display:flex; gap:16px; font-size:12px; color:var(--muted); flex-wrap:wrap; }
main { max-width:1440px; margin:0 auto; padding:22px; }
.cluster-section { margin:0 0 30px; }
.cluster-header { display:flex; align-items:baseline; gap:10px; font-size:17px; margin:0 0 12px; flex-wrap:wrap; }
.cluster-desc { color:var(--muted); font-size:12px; font-weight:normal; }
.hosts-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(330px,1fr)); gap:16px; }
.host-card, .cross-host-card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:14px 16px; box-shadow:0 1px 2px rgba(0,0,0,.04); }
.host-card.kp, .cross-host-card.kp, .service-row.kp { border-color:var(--kp); box-shadow:0 0 0 1px var(--kp) inset; }
.card-title { display:flex; align-items:center; gap:8px; margin:0 0 8px; font-size:15px; flex-wrap:wrap; }
.card-title .nm { font-weight:700; }
.facts { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }
.fact { background:#edf1f7; border-radius:6px; padding:2px 8px; font-size:12px; color:#374151; }
.fact .k { color:var(--muted); }
.services { border-top:1px dashed var(--border); padding-top:8px; margin-top:4px; }
.services-title { font-size:12px; color:var(--muted); margin:0 0 6px; }
.service-row { display:flex; align-items:center; gap:8px; padding:4px 6px; border-radius:8px; font-size:13px; flex-wrap:wrap; margin:2px 0; }
.service-row:nth-child(odd) { background:#fafbfd; }
.svc-name { font-weight:600; }
.svc-type { background:#eef1f6; border-radius:5px; padding:1px 7px; font-size:11px; color:#374151; }
.svc-endpoint { color:var(--muted); font-size:12px; font-family:ui-monospace,Menlo,Consolas,monospace; }
.no-services { color:var(--muted); font-size:12px; font-style:italic; }
.env-badge { border-radius:5px; padding:1px 8px; font-size:11px; color:#fff; }
.env-local { background:#2563eb; } .env-test { background:#d97706; } .env-dev { background:#ea580c; } .env-prod { background:#dc2626; } .env-default { background:#6b7280; }
.status-pill { display:inline-flex; align-items:center; gap:5px; border-radius:99px; padding:1px 9px; font-size:11px; }
.status-running { background:#dcfce7; color:#15803d; } .status-stopped { background:#e5e7eb; color:#4b5563; } .status-default { background:#fef3c7; color:#92400e; }
.kp-mark { color:var(--kp); font-size:12px; font-weight:700; }
.detail-toggle { border:1px solid var(--border); background:#fff; border-radius:6px; padding:2px 10px; font-size:12px; cursor:pointer; color:var(--accent); }
.detail-toggle:hover { background:#f0f4ff; }
.detail-box { display:none; margin-top:10px; padding:10px; background:#f8fafc; border:1px solid var(--border); border-radius:8px; font-size:12px; max-height:340px; overflow:auto; }
.detail-box.open { display:block; }
.kv-row { display:flex; gap:8px; padding:2px 0; border-bottom:1px dotted #e5e7eb; }
.kv-key { color:var(--muted); min-width:130px; flex-shrink:0; }
.kv-val { word-break:break-all; }
.cross-section { margin-bottom:30px; }
.cross-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(270px,1fr)); gap:14px; }
.kp-chains { display:flex; flex-direction:column; gap:10px; }
.kp-chain { display:flex; align-items:center; gap:8px; flex-wrap:wrap; font-size:13px; }
.kp-node { background:#fff; border:1px solid var(--kp); color:var(--kp); border-radius:6px; padding:2px 10px; }
.kp-arrow { color:var(--muted); }
footer { padding:18px 22px 34px; color:var(--muted); font-size:12px; text-align:center; }
.muted { color:var(--muted); }
.empty-note { color:var(--muted); font-style:italic; }
"""

_JS = """\
(function () {
  "use strict";
  var DETAILS = null;
  function getDetails() {
    if (DETAILS !== null) return DETAILS;
    var el = document.getElementById("topo-details");
    if (!el) { DETAILS = {}; return DETAILS; }
    try { DETAILS = JSON.parse(el.textContent) || {}; } catch (e) { DETAILS = {}; }
    return DETAILS;
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function renderValue(v) {
    if (v === null || v === undefined) return '<span class="muted">-</span>';
    if (Array.isArray(v)) {
      if (v.length === 0) return '<span class="muted">[]</span>';
      return v.map(renderValue).join(", ");
    }
    if (typeof v === "object") {
      var keys = Object.keys(v);
      if (keys.length === 0) return '<span class="muted">{}</span>';
      return '<div class="kv">' + keys.map(function (k) {
        return '<div class="kv-row"><span class="kv-key">' + esc(k) +
               '</span><span class="kv-val">' + renderValue(v[k]) + '</span></div>';
      }).join("") + '</div>';
    }
    return esc(v);
  }
  function toggleDetail(btn) {
    var card = btn.closest("[data-detail-key]");
    var box = btn.parentNode.querySelector(".detail-box");
    if (!box) return;
    if (box.classList.contains("open")) {
      box.classList.remove("open");
      btn.textContent = "详情 ▸";
      return;
    }
    var data = card ? getDetails()[card.getAttribute("data-detail-key")] : null;
    if (!data) {
      box.innerHTML = '<span class="muted">无详情数据</span>';
    } else {
      box.innerHTML = renderValue(data);
    }
    box.classList.add("open");
    btn.textContent = "详情 ▾";
  }
  function matches(el, q) {
    if (!q) return true;
    var hay = ((el.getAttribute("data-name") || "") + " " +
               (el.getAttribute("data-type") || "") + " " +
               (el.getAttribute("data-env") || "")).toLowerCase();
    return hay.indexOf(q) !== -1;
  }
  function applyFilter() {
    var input = document.getElementById("topo-search");
    var q = (input ? input.value : "").trim().toLowerCase();
    document.querySelectorAll(".host-card").forEach(function (card) {
      var anySvc = false;
      card.querySelectorAll(".service-row").forEach(function (s) {
        var m = matches(s, q);
        s.style.display = m ? "" : "none";
        if (m) anySvc = true;
      });
      card.style.display = (matches(card, q) || anySvc) ? "" : "none";
    });
    document.querySelectorAll(".cross-host-card").forEach(function (c) {
      c.style.display = matches(c, q) ? "" : "none";
    });
    document.querySelectorAll(".cluster-section").forEach(function (sec) {
      var any = false;
      sec.querySelectorAll(".host-card, .cross-host-card").forEach(function (h) {
        if (h.style.display !== "none") any = true;
      });
      sec.style.display = any ? "" : "none";
    });
  }
  document.addEventListener("DOMContentLoaded", function () {
    var input = document.getElementById("topo-search");
    if (input) input.addEventListener("input", applyFilter);
    document.querySelectorAll(".detail-toggle").forEach(function (b) {
      b.addEventListener("click", function () { toggleDetail(b); });
    });
  });
})();
"""


def _env_class(env: str) -> str:
    env = str(env or "").strip().lower()
    return f"env-{env}" if env in ("local", "test", "dev", "prod") else "env-default"


def _status_class(status: str) -> str:
    status = str(status or "").strip().lower()
    if status in ("running", "up", "healthy", "ok"):
        return "status-running"
    if status in ("stopped", "down", "offline", "error", "failed"):
        return "status-stopped"
    return "status-default"


def _env_badge(env: str) -> str:
    if not str(env or "").strip():
        return ""
    return f'<span class="env-badge {_env_class(env)}">{_esc(env)}</span>'


def _status_pill(status: str) -> str:
    if not str(status or "").strip():
        return ""
    return f'<span class="status-pill {_status_class(status)}">● {_esc(status)}</span>'


def _facts_html(card: Dict[str, Any]) -> str:
    items: List[str] = []
    for key in ("endpoint", "role", "runtime", "port", "owner", "type"):
        value = card.get(key)
        if str(value or "").strip():
            items.append(
                f'<span class="fact"><span class="k">{_esc(key)}</span> {_esc(value)}</span>')
    ports = card.get("ports") or []
    if ports:
        items.append(
            f'<span class="fact"><span class="k">ports</span> '
            f'{_esc(",".join(str(p) for p in ports))}</span>')
    return f'<div class="facts">{"".join(items)}</div>' if items else ""


def _detail_block(key: str, detail: Optional[Dict[str, Any]]) -> str:
    toggle = (
        f'<button type="button" class="detail-toggle" data-detail-key="{_esc(key)}">详情 ▸</button>'
        if detail else ""
    )
    return (
        f'<div class="detail-box"></div>'
        f'{toggle}'
    )


def _service_rows(host: Dict[str, Any]) -> str:
    if host.get("services_missing") and not host["services"]:
        return '<div class="no-services">无服务数据</div>'
    rows: List[str] = []
    for svc in host["services"]:
        card = svc["card"]
        kp = '<span class="kp-mark">🔗</span>' if card.get("on_key_path") else ""
        badge = _status_pill(card.get("status"))
        endpoint = (
            f'<span class="svc-endpoint">{_esc(card["endpoint"])}</span>'
            if str(card.get("endpoint") or "").strip() else "")
        detail_key = f"service:{host['card']['name']}:{card['name']}"
        rows.append(
            '<div class="service-row" data-name="%s" data-type="%s" data-env="%s">'
            '<span class="svc-name">%s</span>%s'
            '<span class="svc-type">%s</span>%s%s%s</div>' % (
                _esc(card["name"]), _esc(card.get("type") or ""), _esc(card.get("env") or ""),
                _esc(card["name"]), kp,
                _esc(card.get("type") or ""), endpoint, badge,
                _detail_block(detail_key, svc.get("detail")),
            )
        )
    return "\n".join(rows) if rows else '<div class="no-services">无服务数据</div>'


def _host_card_html(host: Dict[str, Any]) -> str:
    card = host["card"]
    kp = '<span class="kp-mark">🔗 关键链路</span>' if card.get("on_key_path") else ""
    services_title = (
        f'<div class="services-title">服务（{len(host["services"])}）</div>'
        if host["services"] else '<div class="services-title">服务</div>')
    return (
        '<article class="host-card" data-name="%s" data-type="host" data-env="%s">'
        '<h3 class="card-title"><span class="nm">%s</span>%s%s%s</h3>'
        '%s'
        '<div class="services">%s%s</div>'
        '%s'
        '</article>' % (
            _esc(card["name"]), _esc(card.get("env") or ""),
            _esc(card["name"]), _env_badge(card.get("env")),
            _status_pill(card.get("status")), kp,
            _facts_html(card),
            services_title, _service_rows(host),
            _detail_block(f"host:{card['name']}", host.get("detail")),
        )
    )


def _cross_card_html(cross: Dict[str, Any]) -> str:
    card = cross["card"]
    kp = '<span class="kp-mark">🔗</span>' if card.get("on_key_path") else ""
    return (
        '<article class="cross-host-card" data-name="%s" data-type="%s" data-env="%s">'
        '<h3 class="card-title"><span class="nm">%s</span>%s%s%s</h3>'
        '%s'
        '%s'
        '</article>' % (
            _esc(card["name"]), _esc(card.get("type") or ""), _esc(card.get("env") or ""),
            _esc(card["name"]), _env_badge(card.get("env")),
            _status_pill(card.get("status")), kp,
            _facts_html(card),
            _detail_block(f"cross:{card['name']}", cross.get("detail")),
        )
    )


def _cluster_sections_html(view: Dict[str, Any]) -> str:
    """按集群分组渲染主机卡片；无 cluster 字段的主机归入 default 组。"""
    by_cluster: Dict[str, List[Dict[str, Any]]] = {}
    for host in view["hosts"]:
        by_cluster.setdefault(host["card"]["cluster"], []).append(host)
    cluster_meta = {c["name"]: c for c in view["clusters"]}

    order: List[str] = []
    for c in view["clusters"]:
        if c["name"] not in order:
            order.append(c["name"])
    for name in by_cluster:
        if name not in order:
            order.append(name)

    sections: List[str] = []
    for name in order:
        hosts = by_cluster.get(name, [])
        meta = cluster_meta.get(name, {})
        if not hosts:
            continue
        desc = meta.get("description") or ""
        desc_html = f'<span class="cluster-desc">— {_esc(desc)}</span>' if desc else ""
        cluster_status = _status_pill(meta.get("status"))
        cards = "\n".join(_host_card_html(h) for h in hosts)
        sections.append(
            '<section class="cluster-section" data-cluster="%s">'
            '<h2 class="cluster-header">🖥️ 集群 %s%s%s'
            '<span class="cluster-desc muted">（%d 台主机）</span></h2>'
            '<div class="hosts-grid">%s</div></section>' % (
                _esc(name), _esc(name), desc_html, cluster_status, len(hosts), cards,
            )
        )
    return "\n".join(sections)


def _cross_section_html(view: Dict[str, Any]) -> str:
    if not view["cross_host"]:
        return ""
    cards = "\n".join(_cross_card_html(c) for c in view["cross_host"])
    return (
        '<section class="cross-section" id="cross-host">'
        '<h2 class="cluster-header">🔀 跨主机实体（%d）</h2>'
        '<div class="cross-grid">%s</div></section>' % (len(view["cross_host"]), cards)
    )


def _key_paths_section_html(view: Dict[str, Any]) -> str:
    chains = view["key_paths"]
    if not chains:
        return ""
    rows: List[str] = []
    for chain in chains:
        nodes = []
        for i, name in enumerate(chain):
            if i:
                nodes.append('<span class="kp-arrow">→</span>')
            nodes.append(f'<span class="kp-node">{_esc(name)}</span>')
        rows.append('<div class="kp-chain">%s</div>' % "".join(nodes))
    return (
        '<section class="cross-section" id="key-paths">'
        '<h2 class="cluster-header">🔗 关键链路（%d）</h2>'
        '<div class="kp-chains">%s</div>'
        '<p class="muted" style="font-size:12px">链上实体在页面中以琥珀色边框高亮。</p>'
        '</section>' % (len(chains), "\n".join(rows))
    )


def render_html(view: Dict[str, Any]) -> str:
    """把视图模型渲染成完整自包含 HTML 页面（内联 CSS+JS，零外部依赖）。"""
    root = view.get("data_root", "")
    root_display = _display_path(Path(root)) if root else ""
    version = view.get("version")
    version_html = f"schema v{_esc(version)}" if version is not None else "schema 未知"
    sources = "、".join(view.get("sources") or []) or "未标注"
    envs = "、".join(view.get("environments") or []) or "-"
    n_hosts = len(view["hosts"])
    n_svc = sum(len(h["services"]) for h in view["hosts"])
    n_cross = len(view["cross_host"])
    n_clusters = len(view["clusters"])
    n_chains = len(view["key_paths"])
    n_kp = len(view.get("key_path_entity_names") or [])

    details_json = json.dumps(view.get("details") or {}, ensure_ascii=False)
    details_json = (details_json.replace("<", "\\u003c")
                                 .replace(">", "\\u003e")
                                 .replace("&", "\\u0026"))

    parts: List[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="zh-CN">')
    parts.append("<head>")
    parts.append('<meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append(f"<title>Vigil 拓扑视图 — {_esc(root_display)}</title>")
    parts.append(f"<style>\n{_CSS}\n</style>")
    parts.append("</head>")
    parts.append("<body>")
    parts.append("<header>")
    parts.append("<h1>🛰️ Vigil 拓扑视图</h1>")
    parts.append(
        f'<div class="meta">生成时间：{_esc(view["generated_at"])}'
        f' ｜ 数据根：{_esc(root_display)}（{version_html}）'
        f' ｜ 数据来源：{_esc(sources)}'
        f' ｜ 环境：{_esc(envs)}</div>')
    parts.append(
        f'<div class="meta">统计：{n_clusters} 集群 · {n_hosts} 主机 · {n_svc} 服务'
        f' · {n_cross} 跨主机实体 · {n_chains} 条关键链路（{n_kp} 个实体）</div>')
    parts.append('<div class="controls">')
    parts.append('<input id="topo-search" type="search" placeholder="搜索 name / type / env…" autocomplete="off">')
    parts.append(
        '<div class="legend">'
        '<span><span class="status-pill status-running">● running</span> 运行中</span>'
        '<span><span class="status-pill status-stopped">● stopped</span> 已停止</span>'
        '<span><span class="status-pill status-default">● 其他</span> 未知/其他</span>'
        '<span><span class="kp-node" style="padding:1px 6px">实体</span> 关键链路</span>'
        '</div>')
    parts.append("</div>")
    parts.append("</header>")
    parts.append("<main>")
    if view["hosts"]:
        parts.append(_cluster_sections_html(view))
    else:
        parts.append('<p class="empty-note">拓扑表暂无主机（hosts 段为空）。</p>')
    parts.append(_cross_section_html(view))
    parts.append(_key_paths_section_html(view))
    parts.append("</main>")
    parts.append("<footer>")
    parts.append(
        f'由 <code>vigil topo export --html</code> 生成（{_esc(view["generated_at"])}）。'
        '凭据段已过滤，仅含非敏感字段。')
    parts.append("</footer>")
    parts.append(f'<script type="application/json" id="topo-details">{details_json}</script>')
    parts.append(f"<script>\n{_JS}\n</script>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def run(args) -> int:
    """``vigil topo export``：读三层拓扑 → 渲染 → 写自包含 HTML。"""
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except Exception as exc:
        print(f"✗ 无法解析 Vigil 数据根：{exc}", file=sys.stderr)
        return 2

    view = build_view(home)
    if view is None:
        print(
            f"✗ 拓扑表不存在或为空（{_display_path(home / 'topology.yaml')}）。"
            "先运行 vigil topo-discover 发现主机。",
            file=sys.stderr,
        )
        return 2

    out_path = Path(getattr(args, "output", None) or (home / "topology-view.html")).expanduser()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = render_html(view)
        out_path.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        print(f"✗ 写入失败：{out_path}（{exc}）", file=sys.stderr)
        return 2

    print(
        f"✓ 拓扑视图已导出：{_display_path(out_path)}（{len(rendered)} 字节，"
        f"集群 {len(view['clusters'])} / 主机 {len(view['hosts'])} / "
        f"跨主机 {len(view['cross_host'])}）"
    )
    return 0


def run_reset(args) -> int:
    """``vigil topo reset``：清空全部拓扑数据（回到未初始化状态）。

    破坏性操作：交互确认（提示将清空 N 个实体/主机，y/N）+ 审计记录（谁/何时/
    清空，复用 trajectory）；命令直接操作文件，不走 terminal 命令裁决（正规
    入口，同 topo_update 语义）。reset 后 topo_query 返回空/未初始化，后续按
    "矩阵必须存在/目标解析"正常流程（从 vigil topo-discover 重新登记）。
    """
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except Exception as exc:
        print(f"✗ 无法解析 Vigil 数据根：{exc}", file=sys.stderr)
        return 2
    from tools.topo_tools import load_topology, topo_reset, topology_entity_count

    topo = load_topology(home)
    if topo is None:
        print("拓扑表不存在或为空——已处于未初始化状态，无需 reset。")
        return 0
    counts = {
        "hosts": len([h for h in (topo.get("hosts") or []) if isinstance(h, dict)]),
        "clusters": len([c for c in (topo.get("clusters") or []) if isinstance(c, dict)]),
        "entities": topology_entity_count(home),
    }
    if not getattr(args, "yes", False):
        try:
            answer = input(
                f"⚠️ 将清空全部拓扑数据（{counts['hosts']} 主机 / "
                f"{counts['clusters']} 集群 / {counts['entities']} 实体 + 拓扑目录），"
                "回到未初始化状态。确认？[y/N] "
            )
        except EOFError:
            print("已取消（无交互输入）。", file=sys.stderr)
            return 1
        if str(answer).strip().lower() not in ("y", "yes"):
            print("已取消。", file=sys.stderr)
            return 1

    result = topo_reset(home)
    if not result.get("ok"):
        print(f"✗ {result.get('error') or '拓扑清空失败'}", file=sys.stderr)
        return 2
    _audit_topo_reset(result)
    removed = "、".join(result.get("removed") or [])
    print(
        f"✓ 拓扑已清空（{result.get('hosts', 0)} 主机 / {result.get('clusters', 0)} "
        f"集群 / {result.get('entities', 0)} 实体；删除 {removed}），回到未初始化"
        "状态（已审计）。后续从 vigil topo-discover 重新登记。"
    )
    return 0


def _audit_topo_reset(result: Dict[str, Any]) -> None:
    """清空即审计（best-effort：失败只记日志，绝不阻断清空）。"""
    try:
        import getpass
        from agent.trajectory import record_event
        record_event(
            type="topo_reset",
            session_id="topo-cli",
            tool="topology",
            action="reset",
            result=(f"清空 {result.get('hosts', 0)} 主机 / {result.get('clusters', 0)} "
                    f"集群 / {result.get('entities', 0)} 实体"),
            approval="",
            meta={
                "source": "cli",
                "operator": getpass.getuser(),
                "removed": result.get("removed") or [],
                "hosts": result.get("hosts", 0),
                "clusters": result.get("clusters", 0),
                "entities": result.get("entities", 0),
            },
        )
    except Exception:
        import logging
        logging.getLogger(__name__).debug("topo reset audit event failed", exc_info=True)


def build_topo_export_parser(subparsers, *, cmd_topo_export: Callable,
                             cmd_topo_reset: Callable) -> None:
    """Attach the ``topo`` command (with its ``export`` action) to ``subparsers``."""
    topo_parser = subparsers.add_parser(
        "topo",
        help="拓扑表：导出 HTML 视图（vigil topo export）或清空重建（vigil topo reset）",
        description=(
            "拓扑表管理：export 把三层拓扑渲染成单个自包含 HTML 文件（零 CDN / "
            "零 JS 库，离线可开）；reset 清空全部拓扑数据回到未初始化（从 0 重建"
            "拓扑的正规入口，交互确认 + 审计）。用法：vigil topo export [-o 输出"
            "路径] / vigil topo reset [--yes]。"
        ),
    )
    topo_sub = topo_parser.add_subparsers(dest="topo_command", metavar="{export,reset}")

    export_parser = topo_sub.add_parser(
        "export",
        help="导出拓扑 HTML 视图（默认 <数据根>/topology-view.html）",
        description=(
            "导出拓扑表 HTML 可视化视图：集群分组 + 主机卡片 + 服务列表 + "
            "跨主机实体 + 关键链路（key_paths）高亮 + 状态着色 + 搜索/详情展开。"
            "输出单文件自包含 HTML，file:// 直接打开；凭据段（credential/ref/user/"
            "密码）一律过滤，不进 HTML。"
        ),
    )
    export_parser.add_argument(
        "--html", action="store_true",
        help="导出 HTML 视图（当前唯一格式，可省略——export 默认即 HTML）",
    )
    export_parser.add_argument(
        "-o", "--output", default=None, metavar="PATH",
        help="输出文件路径（默认 <数据根>/topology-view.html）",
    )
    export_parser.set_defaults(func=cmd_topo_export)

    reset_parser = topo_sub.add_parser(
        "reset",
        help="清空全部拓扑数据（回到未初始化；与 vigil matrix reset 回退模板不同）",
        description=(
            "清空全部拓扑数据（topology.yaml + entities/ + services/ + hosts/ + "
            "hardware/ 等拓扑相关文件，回到未初始化状态）。破坏性操作：交互确认"
            "（y/N）+ 审计记录；命令直接操作文件，不走 terminal 命令裁决（正规"
            "入口，同 topo_update 语义）。命名语义：与 vigil matrix reset（回退"
            "模板）不同——topo reset = 清空到未初始化。reset 后 topo_query 返回"
            "空/未初始化，从 vigil topo-discover 重新登记。"
        ),
    )
    reset_parser.add_argument(
        "--yes", "-y", action="store_true",
        help="跳过交互确认（脚本场景；交互使用不推荐）",
    )
    reset_parser.set_defaults(func=cmd_topo_reset)

    def _topo_help(_args) -> int:
        topo_parser.print_help()
        return 0

    topo_parser.set_defaults(func=_topo_help)


def main(argv: Optional[List[str]] = None) -> int:
    """独立入口（测试/直接调用）：argv → Namespace → run（export/reset）。"""
    argv = list(argv or [])
    if argv and argv[0] == "reset":
        from types import SimpleNamespace
        yes = "-y" in argv or "--yes" in argv
        return run_reset(SimpleNamespace(yes=yes))
    parser = argparse.ArgumentParser(
        prog="vigil topo export",
        description=(
            "导出拓扑表 HTML 可视化视图（自包含，零外部依赖，离线可开）。"
        ),
    )
    parser.add_argument("--html", action="store_true",
                        help="导出 HTML 视图（当前唯一格式）")
    parser.add_argument("-o", "--output", default=None, metavar="PATH",
                        help="输出文件路径（默认 <数据根>/topology-view.html）")
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
