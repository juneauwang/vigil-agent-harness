"""内置 digest（ops.digest）与 builtin:digest 的测试。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ops import digest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DB = ROOT / "tests" / "fixtures" / "weekly_digest" / "state.db"
TZ = timezone(timedelta(hours=8))
WINDOW = ("2026-10-20", "2026-10-27")


def _args(**overrides):
    base = {
        "window": None, "week": None, "since": None, "until": None,
        "db": None, "state_dir": None, "format": "facts", "out": None,
        "publish": None, "exclude_job_id": None, "locale": "zh",
    }
    base.update(overrides)
    from types import SimpleNamespace
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------

def test_parse_window_iso_week():
    since, until, label = digest.parse_window(
        _args(week="2026-W43"), datetime(2026, 10, 27, 12, tzinfo=TZ)
    )
    assert since.isoformat() == "2026-10-19T00:00:00+08:00"
    assert until.isoformat() == "2026-10-26T00:00:00+08:00"
    assert label == "2026-W43"


def test_parse_window_rolling_and_2w():
    now = datetime(2026, 10, 27, 12, tzinfo=TZ)
    since, until, label = digest.parse_window(_args(window="7d"), now)
    assert until == now
    assert since == now - timedelta(days=7)
    assert label == "2026-W44"

    since2, until2, _ = digest.parse_window(_args(window="2w"), now)
    assert until2 - since2 == timedelta(weeks=2)


def test_parse_window_rejects_bad_input():
    with pytest.raises(ValueError):
        digest.parse_window(_args(window="nope"), datetime.now(TZ))
    with pytest.raises(ValueError):
        digest.parse_window(_args(since="2026-10-01"), datetime.now(TZ))
    with pytest.raises(ValueError):
        digest.parse_window(
            _args(since="2026-10-02", until="2026-10-01"), datetime.now(TZ)
        )
    with pytest.raises(ValueError):
        digest.parse_window(
            _args(week="2026-W43", window="7d"), datetime.now(TZ)
        )


# ---------------------------------------------------------------------------
# 写动词白名单 / 文件用途句
# ---------------------------------------------------------------------------

def test_write_verb_whitelist():
    assert digest.write_verb("helm show chart ./chart") is None
    assert digest.write_verb("helm upgrade app ./chart -n ns") == "helm:upgrade"
    assert digest.write_verb("kubectl get pods -A") is None
    assert digest.write_verb("kubectl apply -f dep.yaml") == "kubectl:apply"
    assert digest.write_verb("docker ps") is None
    assert digest.write_verb("docker compose up -d") == "compose:up"


def test_terminal_classification_not_action_classifier():
    # action_classifier 会把 helm show 判成 restore；digest 必须保持只读。
    assert digest.classify_terminal("helm show chart ./chart") is None
    item = digest.classify_terminal("helm upgrade app ./chart")
    assert item["kind"] == "命令"
    assert item["target"] == "helm:upgrade"

    # curl GET 只读；带 --data 才是写。
    assert digest.classify_terminal("curl -s http://127.0.0.1:8080/health") is None
    assert digest.classify_terminal("curl -X POST http://127.0.0.1:8080/api")["kind"] == "命令"


def test_curl_flags_are_case_sensitive():
    # -D 是 dump-header（只读探测），-d 才是 data（写）。整串 (?i) 会把 -D 误判成 -d。
    assert digest.classify_terminal(
        "curl -s -o /dev/null -D - -m 8 http://47.120.50.110:30443/v2/ | head -12"
    ) is None
    assert digest.classify_terminal("curl -sI http://127.0.0.1:8080/login") is None
    assert digest.classify_terminal("curl -s -d 'a=1' http://x/api")["kind"] == "命令"
    assert digest.classify_terminal("curl --data-binary @f.json http://x/api")["kind"] == "命令"
    assert digest.classify_terminal("curl -F file=@a.png http://x/up")["kind"] == "命令"
    # grep 的模式串不是命令：`grep 'scp |ssh'` 不能算写操作。
    assert digest.classify_terminal("grep -rn 'scp \\|ssh -\\|SSH_KEY' ~/.vigil") is None
    # ssh 包装的远程写操作要认出来（引号内段首匹配）。
    assert digest.classify_terminal(
        "ssh root@h 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; kubectl apply -f dep.yaml'"
    )["target"] == "kubectl:apply"


def test_purpose_of_prefers_comment_then_yaml():
    assert digest.purpose_of("# 备份脚本\nset -e\n") == "备份脚本"
    assert digest.purpose_of("kind: Deployment\nmetadata:\n  name: web\n") == "kind: Deployment"
    assert digest.purpose_of("") == "空内容"


# ---------------------------------------------------------------------------
# 自身排除
# ---------------------------------------------------------------------------

def _db_copy(tmp_path: Path) -> Path:
    db = tmp_path / "state.db"
    shutil.copy2(FIXTURE_DB, db)
    return db


def _add_session(con, sid, source, parent=None, started=None, title=None):
    started = started or datetime(2026, 10, 26, 12, tzinfo=TZ)
    con.execute(
        """
        INSERT INTO sessions (id, source, title, parent_session_id, started_at,
                              ended_at, end_reason, status, message_count,
                              tool_call_count, input_tokens, output_tokens)
        VALUES (?, ?, ?, ?, ?, ?, 'turn_complete', 'ended', 1, 0, 0, 0)
        """,
        (sid, source, title, parent, started.timestamp(),
         (started + timedelta(minutes=5)).timestamp()),
    )


def _add_message(con, sid, role, content=None, tool_calls=None):
    con.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            sid, role, content,
            json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
            datetime(2026, 10, 26, 12, 5, tzinfo=TZ).timestamp(),
        ),
    )


def _seed_exclusion_db(tmp_path: Path) -> Path:
    db = _db_copy(tmp_path)
    con = sqlite3.connect(db)
    try:
        # 只要本用例的 4 个会话，避免 fixture 自带会话干扰排除断言。
        con.execute("DELETE FROM messages")
        con.execute("DELETE FROM sessions")
        _add_session(con, "cron_jobx_20261026_120000", "cron")
        _add_session(
            con, "20261026_120100_sub001", "subagent",
            parent="cron_jobx_20261026_120000",
        )
        _add_session(con, "cron_other_20261026_130000", "cron")
        _add_session(con, "web_manual_20261026_140000", "web")

        _add_message(con, "cron_jobx_20261026_120000", "user", "周报自身任务")
        _add_message(con, "20261026_120100_sub001", "user", "派生子任务")
        _add_message(con, "cron_other_20261026_130000", "user", "别的定时任务")
        _add_message(
            con, "cron_other_20261026_130000", "assistant", "",
            tool_calls=[{"function": {"name": "terminal", "arguments": json.dumps(
                {"command": "kubectl apply -f dep.yaml"}
            )}}],
        )
        _add_message(con, "web_manual_20261026_140000", "user", "用户手工任务")
        con.commit()
    finally:
        con.close()
    return db


def test_exclusion_with_job_id_keeps_other_cron(tmp_path):
    db = _seed_exclusion_db(tmp_path)
    since, until, _ = digest.parse_window(
        _args(since=WINDOW[0], until=WINDOW[1]), datetime(2026, 10, 27, tzinfo=TZ)
    )
    facts = digest.collect(db, since, until, job_id="jobx")
    ids = {t["session"] for t in facts["tasks"]}
    assert "cron_jobx_20261026_120000" not in ids
    assert "20261026_120100_sub001" not in ids
    assert "cron_other_20261026_130000" in ids
    assert "web_manual_20261026_140000" in ids
    assert facts["totals"]["excluded_pipeline_sessions"] == 2


def test_exclusion_without_job_id_drops_all_cron(tmp_path):
    db = _seed_exclusion_db(tmp_path)
    since, until, _ = digest.parse_window(
        _args(since=WINDOW[0], until=WINDOW[1]), datetime(2026, 10, 27, tzinfo=TZ)
    )
    facts = digest.collect(db, since, until)
    ids = {t["session"] for t in facts["tasks"]}
    assert ids == {"web_manual_20261026_140000"}
    assert facts["totals"]["excluded_pipeline_sessions"] == 3


def test_extra_job_id_excludes_historical_job(tmp_path):
    db = _seed_exclusion_db(tmp_path)
    since, until, _ = digest.parse_window(
        _args(since=WINDOW[0], until=WINDOW[1]), datetime(2026, 10, 27, tzinfo=TZ)
    )
    facts = digest.collect(db, since, until, extra_job_ids=["other"])
    ids = {t["session"] for t in facts["tasks"]}
    assert ids == {"web_manual_20261026_140000"}
    assert facts["totals"]["excluded_pipeline_sessions"] == 3


# ---------------------------------------------------------------------------
# 渲染 / CLI 退出码 / 发布
# ---------------------------------------------------------------------------

def test_render_md_groups_by_day(tmp_path):
    db = _seed_exclusion_db(tmp_path)
    since, until, _ = digest.parse_window(
        _args(since=WINDOW[0], until=WINDOW[1]), datetime(2026, 10, 27, tzinfo=TZ)
    )
    facts = digest.collect(db, since, until, job_id="jobx")
    md = digest.render_md(facts)
    assert md.startswith("# Vigil 作业周报 2026-W44")
    assert "## 1. 本周做了什么" in md
    assert "### 10-26" in md
    assert "## 2. 变更明细" in md
    assert "kubectl:apply" in md


def test_main_exit_codes(tmp_path):
    assert digest.main(["--window", "bad", "--db", str(FIXTURE_DB)]) == 2
    assert digest.main(["--db", str(tmp_path / "missing.db")]) == 3


def test_publish_bookstack_without_config_returns_4(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path / "empty-home"))
    code = digest.main([
        "--db", str(FIXTURE_DB),
        "--since", WINDOW[0], "--until", WINDOW[1],
        "--format", "md", "--publish", "bookstack",
        "--state-dir", str(tmp_path / "state"),
    ])
    assert code == 4
    assert (tmp_path / "state" / "reports" / "Vigil-作业周报-2026-W44.md").is_file()


def test_publish_file_writes_report(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path / "empty-home"))
    code = digest.main([
        "--db", str(FIXTURE_DB),
        "--since", WINDOW[0], "--until", WINDOW[1],
        "--format", "md", "--publish", "file",
        "--state-dir", str(tmp_path / "state"),
    ])
    assert code == 0
    report = tmp_path / "state" / "reports" / "Vigil-作业周报-2026-W44.md"
    assert report.is_file()
    assert report.read_text(encoding="utf-8").startswith("# Vigil 作业周报")


# ---------------------------------------------------------------------------
# builtin:digest
# ---------------------------------------------------------------------------

def test_builtin_script_whitelist():
    from tools.cronjob_tools import _validate_cron_script_path

    assert _validate_cron_script_path("builtin:digest") is None
    error = _validate_cron_script_path("builtin:nope")
    assert error and "未知内置脚本" in error
    assert _validate_cron_script_path("builtin:digest --window 7d") is not None


def test_builtin_digest_runs_in_process(tmp_path):
    from cron.scheduler import _run_job_script

    ok, out = _run_job_script(
        "builtin:digest",
        job={
            "id": "jobx",
            "name": "测试 job",
            "script_args": [
                "--db", str(FIXTURE_DB),
                "--since", WINDOW[0], "--until", WINDOW[1],
                "--format", "facts",
            ],
        },
    )
    assert ok is True, out
    facts = json.loads(out)
    assert facts["window"]["label"] == "2026-W44"
    assert "tasks" in facts and "days" in facts


def test_builtin_unknown_rejected_at_runtime():
    from cron.scheduler import _run_job_script

    ok, out = _run_job_script("builtin:nope")
    assert ok is False
    assert "Unknown builtin script" in out


def test_create_job_stores_script_args(tmp_path, monkeypatch):
    from cron.jobs import create_job

    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    job = create_job(
        prompt="",
        schedule="every 1h",
        name="builtin test",
        script="builtin:digest",
        script_args=["--window", "7d", "--format", "facts"],
        no_agent=True,
    )
    assert job["script"] == "builtin:digest"
    assert job["script_args"] == ["--window", "7d", "--format", "facts"]


# ---------------------------------------------------------------------------
# 蓝图
# ---------------------------------------------------------------------------

def test_weekly_oplog_blueprint_falls_back_without_publish_config(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path / "home"))
    from cron.blueprint_catalog import fill_blueprint, get_blueprint

    spec = fill_blueprint(get_blueprint("weekly-oplog"), {
        "time": "18:00", "day": "friday", "window": "7d",
        "publish": "bookstack", "deliver": "local",
    })
    assert spec["script"] == "builtin:digest"
    assert spec["no_agent"] is True
    assert spec["script_args"][-2:] == ["--publish", "file"]
    assert "未配置 BookStack 发布目标" in spec["prompt"]


def test_weekly_oplog_blueprint_agent_mode_when_configured(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "ops:\n  digest:\n    publish:\n      target: bookstack\n"
        "      bookstack:\n        base_url: http://127.0.0.1:6876\n"
        "        book_id: 1\n        credential_ref: bookstack-api\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIGIL_HOME", str(home))
    from tools.credential_vault import store
    store("bookstack-api", "1:secret")

    from cron.blueprint_catalog import fill_blueprint, get_blueprint

    spec = fill_blueprint(get_blueprint("weekly-oplog"), {
        "time": "18:00", "day": "friday", "window": "14d",
        "publish": "bookstack", "deliver": "local",
    })
    assert spec["script"] == "builtin:digest"
    assert spec.get("no_agent") is not True
    assert spec["script_args"] == ["--window", "14d", "--format", "facts"]
    assert "bookstack" in spec["enabled_toolsets"]


def test_default_window_reads_config(tmp_path, monkeypatch):
    """ops.digest.window 作为「命令行没给窗口」时的缺省。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "ops:\n  digest:\n    window: 14d\n", encoding="utf-8"
    )
    monkeypatch.setenv("VIGIL_HOME", str(home))
    assert digest.default_window() == "14d"

    # 显式参数仍然压倒配置
    code = digest.main(["--db", str(FIXTURE_DB), "--since", WINDOW[0],
                        "--until", WINDOW[1], "--format", "facts"])
    assert code == 0


def test_default_window_fallback_and_bad_value(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(home))
    assert digest.default_window() == "7d"

    (home / "config.yaml").write_text(
        "ops:\n  digest:\n    window: nonsense\n", encoding="utf-8"
    )
    assert digest.default_window() == "7d"

    (home / "config.yaml").write_text(
        "ops:\n  digest:\n    window: 0d\n", encoding="utf-8"
    )
    assert digest.default_window() == "7d"


def test_render_md_uses_prose_but_keeps_facts_tables(tmp_path):
    """模型只交叙述：第 1/3/4 段用 prose，对象表仍逐值来自 facts。"""
    from ops import digest

    facts = {
        "window": {"since": "2026-10-20T00:00:00+08:00", "until": "2026-10-27T00:00:00+08:00", "label": "2026-W43"},
        "totals": {
            "tasks": 1, "changes": 1, "by_kind": {"拓扑": 1},
            "objects": 1, "objects_with_identity": 1, "objects_missing_identity": 0,
            "by_type": {"配置": 1},
            "excluded_pipeline_sessions": 0, "active_days": 1,
        },
        "days": [{"date": "10-21", "task_indexes": [0], "changes": 1, "kinds": {"拓扑": 1}}],
        "tasks": [{
            "when": "10-21 10:00", "session": "chat_x", "source": "web", "asks": ["把网关重启"],
            "change_total": 1, "object_total": 1, "with_identity": 1,
            "end_reason": "turn_complete",
            "changes": [{"kind": "拓扑", "target": "gateway-svc", "detail": "endpoint=1.2.3.4:8080", "why": ""}],
            "objects": [{"type": "配置", "identity": "gateway-svc.yaml", "params": "endpoint=1.2.3.4:8080",
                         "result": "configured", "source": "chat_x#7", "hits": 1}],
            "intents": [{"family": "配置", "label": "配置与服务：gateway-svc.yaml",
                         "objects": [{"type": "配置", "identity": "gateway-svc.yaml",
                                      "params": "endpoint=1.2.3.4:8080", "result": "configured",
                                      "source": "chat_x#7", "hits": 1}],
                         "with_identity": 1, "results": ["configured"]}],
        }],
        "gaps": [],
    }
    prose = {"summary": "- 重启了网关，服务恢复。", "risks": "- 无。", "next": "1. 观察一天。"}
    md = digest.render_md(facts, prose)
    assert "- 重启了网关，服务恢复。" in md
    assert "## 1. 子目标与结果" in md
    assert "## 2. 变更明细（按对象）" in md
    assert "## 3. 未闭环与待跟进" in md
    assert "## 4. 下周期" in md and "1. 观察一天。" in md
    # 对象表的标识/参数必须原样来自 facts，不被 prose 覆盖
    assert "gateway-svc.yaml" in md and "endpoint=1.2.3.4:8080" in md
    # 报告不再出现「调了什么工具/命令」这类过程章节
    assert "执行过的写类命令" not in md
    assert "待补：由撰写者" not in md


def test_render_md_marks_missing_identity_and_coverage(tmp_path):
    """抽不到标识的对象：表里写「标识未记录」，头部给覆盖率，第 3 节列缺口。"""
    from ops import digest

    obj = {"type": "监控面板", "identity": "", "params": "", "result": "", "source": "chat_y#9", "hits": 1}
    facts = {
        "window": {"since": "2026-10-20T00:00:00+08:00", "until": "2026-10-27T00:00:00+08:00", "label": "2026-W43"},
        "totals": {
            "tasks": 1, "changes": 0, "by_kind": {},
            "objects": 1, "objects_with_identity": 0, "objects_missing_identity": 1,
            "by_type": {"监控面板": 1},
            "excluded_pipeline_sessions": 0, "active_days": 1,
        },
        "days": [],
        "tasks": [{
            "when": "10-21 10:00", "session": "chat_y", "source": "web", "asks": ["配面板"],
            "change_total": 0, "object_total": 1, "with_identity": 0, "end_reason": "turn_complete",
            "changes": [], "objects": [obj],
            "intents": [{"family": "监控", "label": "监控接入", "objects": [obj],
                         "with_identity": 0, "results": []}],
        }],
        "gaps": [],
    }
    md = digest.render_md(facts, {})
    assert "0 个带可复核标识" in md
    assert "标识未记录" in md
    assert "1 个对象没有可复核标识" in md


def test_object_from_db_command_extracts_identity():
    from ops import digest

    cmd = 'psql -h node233 -d test_pg -U admin -c "CREATE ROLE xinggh LOGIN SUPERUSER PASSWORD \'x\'"'
    obj = digest._object_from_command(cmd, "CREATE ROLE", "chat_b72a9f5#12")
    assert obj["type"] == "账号与权限"
    assert "xinggh" in obj["identity"] and "node233" in obj["identity"] and "test_pg" in obj["identity"]
    assert "SUPERUSER" in obj["params"]


def test_objects_from_content_extracts_prometheus_job_and_skips_process_scripts():
    from ops import digest

    content = """
serverFiles:
  prometheus.yml:
    scrape_configs:
      - job_name: milvus
        scrape_interval: 15s
        static_configs:
          - targets: ['node233:9091']
"""
    objs = digest._objects_from_content("/opt/prom/prometheus_new.yaml", content, "state=up", "chat_x#3")
    assert objs and objs[0]["type"] == "监控采集"
    assert "milvus" in objs[0]["identity"]
    assert "node233:9091" in objs[0]["params"] and "15s" in objs[0]["params"]
    assert objs[0]["result"] == "target state=up"

    # /tmp 下的诊断/验证脚本是过程产物，不进报告
    assert digest._objects_from_content("/tmp/diag-milvus-rbac.yml", content, "", "chat_x#4") == []
    assert digest._objects_from_content("/tmp/prom-reload-verify.py", "print(1)", "", "chat_x#5") == []


def test_group_into_intents_splits_account_and_monitoring():
    """交付物（runbook/文档/技能）挂到本任务的主族，不单独成一节。"""
    from ops import digest

    objs = [
        {"type": "账号与权限", "identity": "xinggh @ node233/test_pg", "params": "", "result": "", "source": "s#1", "hits": 1},
        {"type": "可复用资产", "identity": "runbook xinggh-superadmin-account", "params": "env=test", "result": "", "source": "s#2", "hits": 1},
        {"type": "文档", "identity": "xinggh-连接说明.md", "params": "", "result": "", "source": "s#3", "hits": 1},
        {"type": "监控采集", "identity": "scrape job milvus", "params": "targets=[node233:9091]", "result": "target state=up", "source": "s#4", "hits": 1},
        {"type": "数据源", "identity": "milvus-prom（uid af123456）", "params": "http://grafana:3000", "result": "", "source": "s#5", "hits": 1},
    ]
    intents = digest.group_into_intents(objs, "建 xinggh 账号并接监控")
    fams = [i["family"] for i in intents]
    assert fams == ["账号", "监控"]
    assert {o["type"] for o in intents[0]["objects"]} == {"账号与权限", "可复用资产", "文档"}
    assert {o["type"] for o in intents[1]["objects"]} == {"监控采集", "数据源"}


def test_render_md_groups_by_day(tmp_path):
    """无 prose 时第 1 段按天列出任务与子目标（模板口径）。"""
    from ops import digest

    facts = {
        "window": {"since": "2026-10-20T00:00:00+08:00", "until": "2026-10-21T00:00:00+08:00", "label": "2026-10-20"},
        "totals": {"tasks": 1, "changes": 1, "by_kind": {"文件": 1},
                   "objects": 2, "objects_with_identity": 2, "objects_missing_identity": 0,
                   "by_type": {"监控采集": 1, "账号与权限": 1},
                   "excluded_pipeline_sessions": 0, "active_days": 1},
        "days": [{"date": "10-20", "task_indexes": [0], "changes": 1, "kinds": {"文件": 1}}],
        "tasks": [{
            "when": "10-20 10:00", "session": "chat_a", "source": "web", "asks": ["建账号和监控"],
            "change_total": 1, "object_total": 2, "with_identity": 2, "end_reason": "turn_complete",
            "changes": [{"kind": "文件", "target": "a.yml", "detail": "写：x", "why": ""}],
            "objects": [],
            "intents": [
                {"family": "账号", "label": "数据库账号：xinggh",
                 "objects": [{"type": "账号与权限", "identity": "xinggh @ node233/test_pg", "params": "",
                              "result": "", "source": "chat_a#3", "hits": 1}],
                 "with_identity": 1, "results": []},
                {"family": "监控", "label": "监控接入：scrape job milvus",
                 "objects": [{"type": "监控采集", "identity": "scrape job milvus",
                              "params": "targets=[node233:9091]", "result": "target state=up",
                              "source": "chat_a#4", "hits": 1}],
                 "with_identity": 1, "results": ["target state=up"]},
            ],
        }],
        "gaps": [],
    }
    md = digest.render_md(facts, {})
    assert "### 10-20 · 建账号和监控" in md
    assert "数据库账号：xinggh" in md
    assert "监控接入：scrape job milvus（1 项：target state=up）" in md


def test_sudo_exec_and_kubectl_and_topo_objects():
    """提权/远程工具与拓扑更新也要能抽成对象（90d 实测里这三类全丢了）。"""
    from ops import digest

    # sudo_exec：host 来自工具参数，命令里没有 -h
    objs = digest.objects_from_call(
        "sudo_exec",
        {"host": "39.106.217.32", "env": "prod", "command": "apt-get install -y openbao"},
        "ok", "chat_x#1",
    )
    assert objs and objs[0]["type"] == "系统服务"
    assert objs[0]["identity"] == "openbao @ 39.106.217.32"

    # kubectl apply：标识取文件名，不是整条命令
    objs = digest.objects_from_call(
        "terminal", {"command": "kubectl apply -f /opt/manifests/alertmanager.yaml"}, "created", "chat_x#2"
    )
    assert objs[0]["identity"] == "alertmanager.yaml"
    assert "kubectl apply" in objs[0]["params"]

    # 拓扑更新
    objs = digest.objects_from_call(
        "topo_update", {"entity": "gateway-svc", "updates": {"endpoint": "1.2.3.4:8080"}},
        "ok", "chat_x#3",
    )
    assert objs[0]["type"] == "拓扑" and objs[0]["identity"] == "gateway-svc"
    assert "endpoint=1.2.3.4:8080" in objs[0]["params"]

    # 同一文件的写 + apply 合成一个对象
    written = digest._objects_from_content(
        "/opt/manifests/alertmanager.yaml", "kind: Deployment\nmetadata:\n  name: am\n", "ok", "s#1")
    assert written and written[0]["identity"] == "alertmanager.yaml"
    merged = digest._coalesce_configs(written + objs_from_call_apply())
    assert len(merged) == 1


def objs_from_call_apply():
    from ops import digest
    return digest.objects_from_call(
        "terminal", {"command": "kubectl apply -f /opt/manifests/alertmanager.yaml"}, "created", "s#2")


def test_e2e_objects_from_real_session_shape(tmp_path):
    """端到端：一条「建账号 + 接监控」的会话，报告要能看出改了什么。

    用最小 state.db 复刻 dogfood 里的真实形态：psql 建账号、prometheus 配置、
    grafana 数据源、runbook、交付文档，外加 /tmp 过程脚本（必须被折叠掉）。
    """
    import json as _json
    import sqlite3 as _sqlite3
    from datetime import datetime, timedelta

    from ops import digest

    db = tmp_path / "state.db"
    conn = _sqlite3.connect(db)
    conn.executescript(
        """
        create table sessions (id text primary key, source text, parent_session_id text,
          started_at real, ended_at real, end_reason text, message_count int,
          tool_call_count int, input_tokens int, output_tokens int);
        create table messages (id integer primary key autoincrement, session_id text,
          role text, content text, tool_call_id text, tool_calls text);
        """
    )
    started = datetime.now(digest.SHANGHAI) - timedelta(hours=3)
    conn.execute(
        "insert into sessions values (?,?,?,?,?,?,?,?,?,?)",
        ("chat_b72a9f5", "web", None, started.timestamp(), started.timestamp() + 3600,
         "turn_complete", 20, 10, 0, 0),
    )

    def call(cid, name, args, result):
        conn.execute(
            "insert into messages (session_id, role, content, tool_calls) values (?,?,?,?)",
            ("chat_b72a9f5", "assistant", "",
             _json.dumps([{"id": cid, "function": {"name": name, "arguments": _json.dumps(args)}}])),
        )
        conn.execute(
            "insert into messages (session_id, role, content, tool_call_id) values (?,?,?,?)",
            ("chat_b72a9f5", "tool", result, cid),
        )

    conn.execute(
        "insert into messages (session_id, role, content) values (?,?,?)",
        ("chat_b72a9f5", "user", "帮我在node233和node234的test-pg和test-milvus上建管理员用户 xinggh，密码放 openbao"),
    )
    call("c1", "terminal",
         {"command": 'psql -h node233 -d test_pg -U admin -c "CREATE ROLE xinggh LOGIN SUPERUSER"'},
         "CREATE ROLE")
    call("c2", "write_file",
         {"path": "/opt/ansible/playbooks/provision-superadmin-account.yml",
          "content": "---\n- name: provision\n  community.postgresql.postgresql_user:\n    name: xinggh\n# CREATE ROLE xinggh; password -> kv/test/pg/xinggh\n"},
         "wrote 12 lines")
    call("c3", "write_file",
         {"path": "/tmp/diag-milvus-rbac.yml", "content": "kind: Pod\nmetadata:\n  name: diag\n"},
         "wrote 3 lines")
    call("c4", "write_file",
         {"path": "/opt/prom/prometheus_new.yml",
          "content": "scrape_configs:\n  - job_name: milvus\n    scrape_interval: 15s\n    static_configs:\n      - targets: ['node233:9091']\n"},
         "wrote 7 lines")
    call("c5", "terminal", {"command": "curl -s http://node233:9090/-/reload -X POST"},
         '{"status":"success"} target state=up')
    call("c6", "terminal",
         {"command": "curl -s -X POST http://grafana:3000/api/datasources -d @ds.json"},
         '{"id":3,"uid":"af123456","name":"milvus-prom","url":"http://node233:9091"}')
    call("c7", "runbook_execute",
         {"runbook": "xinggh-superadmin-account", "env": "test"}, '{"result":"ok"}')
    call("c8", "write_file",
         {"path": "/root/reports/xinggh-连接说明.md",
          "content": "# 数据库账号使用说明（账号：xinggh）\nhost node233 port 5432\n"},
         "wrote 4 lines")
    call("c9", "skill_manage", {"name": "grafana-dashboard-sync", "action": "patch"}, "ok")
    conn.commit()
    conn.close()

    now = datetime.now(digest.SHANGHAI)
    facts = digest.collect(db, now - timedelta(days=1), now)
    totals = facts["totals"]
    assert totals["objects"] >= 6
    assert totals["objects_missing_identity"] == 0
    task = facts["tasks"][0]
    fams = [i["family"] for i in task["intents"]]
    assert fams == ["账号", "监控"], fams
    account_types = {o["type"] for o in task["intents"][0]["objects"]}
    monitor_types = {o["type"] for o in task["intents"][1]["objects"]}
    assert "账号与权限" in account_types and "可复用资产" in account_types and "文档" in account_types
    assert "监控采集" in monitor_types and "数据源" in monitor_types

    md = digest.render_md(facts, {})
    # 报告能看出改了什么（标识 + 参数 + 结果）
    assert "xinggh @ node233/test_pg" in md
    assert "scrape job milvus" in md and "node233:9091" in md and "15s" in md
    assert "target state=up" in md
    assert "milvus-prom（uid af123456）" in md
    assert "runbook xinggh-superadmin-account" in md
    assert "xinggh-连接说明.md" in md
    # 过程脚本不进报告；也没有「调了什么工具」这种章节
    assert "diag-milvus-rbac" not in md
    assert "执行过的写类命令" not in md
    assert "0 个带可复核标识" not in md


def test_load_prose_default_path_and_fallback(tmp_path):
    from ops import digest

    reports = tmp_path / "reports"
    reports.mkdir()
    label = "2026-W43"
    (reports / f"{digest.REPORT_PREFIX}{label}.prose.json").write_text(
        '{"summary": "S", "risks": "R", "next": "N"}', encoding="utf-8"
    )
    prose = digest._load_prose(tmp_path, label, "auto")
    assert prose == {"summary": "S", "risks": "R", "next": "N"}
    # 文件不在 → None（回落模板），且不抛
    assert digest._load_prose(tmp_path, "2026-W99", "auto") is None
    # 显式 none 强制模板
    assert digest._load_prose(tmp_path, label, "none") is None
    # 坏 JSON → None
    (reports / f"{digest.REPORT_PREFIX}2026-W98.prose.json").write_text("{bad", encoding="utf-8")
    assert digest._load_prose(tmp_path, "2026-W98", "auto") is None


def test_builtin_alert_autodispatch_is_whitelisted():
    from cron.builtin_scripts import BUILTIN_SCRIPTS, builtin_name

    assert builtin_name("builtin:alert-autodispatch") == "alert-autodispatch"
    assert BUILTIN_SCRIPTS["alert-autodispatch"] == "hermes_cli.alert_autodispatch"


def test_topo_objects_carry_add_update_approve():
    """拓扑对象的结果列要能看出是新增/更新/审核通过（用户 2026-10-28 指出全是 '—'）。"""
    import json as _json

    from ops import digest

    # discover 未 commit → 不算变更
    assert digest.objects_from_call("topo_discover", {"host": "h", "commit": False}, "{}", "s#1") == []

    # discover commit → 新增（待审），实体名来自 drafts
    payload = _json.dumps({"status": "committed", "drafts": ["laptop-t2ja2ere", "kind-registry"],
                           "appended": 2, "kept": 0})
    objs = digest.objects_from_call("topo_discover", {"host": "h", "commit": True}, payload, "s#2")
    assert [o["identity"] for o in objs] == ["laptop-t2ja2ere", "kind-registry"]
    assert {o["result"] for o in objs} == {"新增（待审）"}

    # 只改 needs_review=false → 审核通过，且不把 needs_review 塞进参数
    objs = digest.objects_from_call(
        "topo_update", {"entity": "argo-server", "updates": {"needs_review": False}}, "ok", "s#3")
    assert objs[0]["result"] == "审核通过"
    assert "needs_review" not in objs[0]["params"]

    # 真改字段 → 更新，参数保留
    objs = digest.objects_from_call(
        "topo_update", {"entity": "laptop-t2ja2ere",
                        "updates": {"type": "host", "endpoint": "172.18.120.67", "needs_review": False}},
        "ok", "s#4")
    assert objs[0]["result"] == "更新"
    assert "endpoint=172.18.120.67" in objs[0]["params"]

    # 同一实体「新增 → 审核通过」合并成一行，两个结果都留着
    merged = digest._coalesce_topo(
        digest.objects_from_call("topo_discover", {"commit": True}, payload, "s#2")[:1]
        + [digest.objects_from_call(
            "topo_update", {"entity": "laptop-t2ja2ere", "updates": {"needs_review": False}},
            "ok", "s#5")[0]]
    )
    assert len(merged) == 1 and "新增（待审）" in merged[0]["result"] and "审核通过" in merged[0]["result"]


def test_topo_delete_object_only_when_deleted():
    """topo_delete 成功 → 拓扑对象「删除（已归档）」；被拦/审批拒绝 → 不记变更。"""
    import json as _json

    from ops import digest

    payload = _json.dumps({"status": "deleted", "entity": "grafana", "env": "dev",
                           "archived": "topology-archive/2026-10-28/grafana.yaml"})
    objs = digest.objects_from_call("topo_delete", {"entity": "grafana", "reason": "迁移"}, payload, "s#1")
    assert objs[0]["type"] == "拓扑" and objs[0]["identity"] == "grafana"
    assert objs[0]["result"] == "删除（已归档）"
    assert "env=dev" in objs[0]["params"] and "grafana.yaml" in objs[0]["params"]

    # 有引用被拦（error）→ 没删成，不进报告
    blocked = _json.dumps({"error": "实体 grafana 仍被引用（1 处）：depsvc(depends_on)"})
    assert digest.objects_from_call("topo_delete", {"entity": "grafana"}, blocked, "s#2") == []
