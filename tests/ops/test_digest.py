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
