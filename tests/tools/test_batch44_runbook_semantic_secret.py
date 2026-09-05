"""批次四十四（§BN/§BL）：runbook 凭据校验误判收口——语义识别替代词面。

覆盖：
  - Task1 放行：-p 是端口/路径/无值 flag 的不算密码（mkdir -p 路径、scp -p
    保时间戳、docker -p 端口、psql -p 端口、-i 私钥路径、DB 工具 -P 大写端口）。
  - Task1 拦截：密码命令白名单（sshpass/mysql/mongosh 等）且值是密码候选
    （短串非数字/路径/变量/引用）→ 仍拦；--password=... 词面严格仍拦。
  - Task2 放行：变量名含 PASS/TOKEN 但值是指向 secret 的路径/引用 →
    不是明文；真明文赋值（PASSWORD=hunter2）仍拦。
  - Task3：拦截错误消息含正确写法引导（topo_query / vssh / <secret:path/field>），
    且值不回显。
  - <secret:...> 占位符放行行为保持。
"""

from __future__ import annotations

import json

import pytest

import hermes_cli.config as hc
from tools.runbook_tools import (
    _find_plaintext_secret,
    _secret_error_hint,
    runbook_create,
)


@pytest.fixture
def rb_home(tmp_path, monkeypatch):
    home = tmp_path / "vigil_home"
    (home / "runbooks").mkdir(parents=True)
    # batch74：v0.1 仅允许 overwrite 存量文件——预置一个 v0.1 存量，
    # 让本套件的 v0.1 凭据语义扫描测试继续走 v0.1 路径。
    (home / "runbooks" / "semantic-secret-check.yaml").write_text(
        "name: semantic-secret-check\ntitle: 存量\nversion: 1\nkind: incident\n"
        "steps:\n  - id: s1\n    title: x\n    commands: [echo old]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        yield home
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


def _load(result: str) -> dict:
    return json.loads(result)


def _incident(**overrides):
    data = {
        "runbook": "semantic-secret-check",
        "title": "语义化凭据校验",
        "triggers": ["semantic secret"],
        "summary": "校验不放行误杀合法命令。",
        "env": "test",
        "kind": "incident",
        "steps": [{"id": "s1", "title": "x", "commands": ["ls"]}],
    }
    data.update(overrides)
    return data


ALLOW_COMMANDS = [
    "mkdir -p /root/user.pem",            # -p 后是路径 → 放行
    "scp -p server:/x /y",                     # scp -p 保时间戳
    "docker run -d -p 8080:80 nginx",          # docker -p 端口发布
    "psql -p 5432 -c 'select 1'",              # psql -p 端口
    "ssh -i /root/user.pem root@host",     # -i 私钥路径（含 pem/-p 形态）
    "mysql -P 3306 -uroot db",                 # DB 工具 -P 大写端口
    "mysql -p 3306 -e 'select 1'",             # 数字值 → 端口
    "redis-cli -p 6379 ping",                  # redis-cli -p 端口
    "VAULT_PASS=secret/data/CSNDC/maas curl x",   # 变量名含 PASS 但是路径引用
    "MY_TOKEN_REF=secret/data/a/b curl x",        # 引用形态 → 非明文
    "docker -P x nginx",                         # -P 大写非密码
]


class TestSemanticValueIdentification:
    @pytest.mark.parametrize("cmd", ALLOW_COMMANDS)
    def test_non_password_forms_allowed(self, cmd):
        assert _find_plaintext_secret(cmd) is None, cmd

    @pytest.mark.parametrize("cmd,key", [
        ("sshpass -p 'hunter2' ssh user@h", "-p"),
        ("sshpass -p hunter2 x", "-p"),
        ("mysql -psecret -e x", "-p"),
        ("mysql -p'secret' -e x", "-p"),
        ("mongosh -p secret", "-p"),
        ("--password=abc", "--password"),
        ("curl -u admin:secret123 http://x", "-u"),
        ("PASSWORD=hunter2", "PASSWORD"),
    ])
    def test_plaintext_passwords_blocked(self, cmd, key):
        assert _find_plaintext_secret(cmd) == key, cmd


class TestErrorHint:
    def test_hint_teaches_correct_channel(self):
        hint = _secret_error_hint("-p")
        assert "明文凭据" in hint
        assert "topo_query" in hint
        assert "vssh" in hint
        assert "<secret:path/field>" in hint

    def test_hint_mentions_plain_arg_is_ok(self):
        hint = _secret_error_hint("PASSWORD")
        assert "本地文件路径/端口/连接参数" in hint


class TestRunbookCreateSemantic:
    def test_allows_legit_port_and_path_forms(self, rb_home):
        data = _incident(steps=[{"id": "s1", "title": "x", "commands": [
            "mkdir -p /root/user.pem",
            "docker run -d -p 8080:80 nginx",
        ]}])
        result = _load(runbook_create(**data, overwrite=True, home=rb_home))
        assert result.get("status") in ("created", "updated"), result

    def test_allows_vault_pass_assignment_ref(self, rb_home):
        data = _incident(steps=[{"id": "s1", "title": "x", "commands": [
            "VAULT_PASS=secret/data/CSNDC/maas envsubst < tmpl > out",
        ]}])
        result = _load(runbook_create(**data, overwrite=True, home=rb_home))
        assert result.get("status") in ("created", "updated"), result

    def test_rejects_plaintext_sshpass(self, rb_home):
        data = _incident(steps=[{"id": "s1", "title": "x", "commands": [
            "sshpass -p 'hunter2' ssh user@host",
        ]}])
        result = _load(runbook_create(**data, overwrite=True, home=rb_home))
        assert "error" in result
        assert "明文凭据" in result["error"]
        assert "topo_query" in result["error"]
        assert "hunter2" not in result["error"]  # 值不回显

    def test_rejects_plaintext_assignment(self, rb_home):
        data = _incident(steps=[{"id": "s1", "title": "x", "commands": [
            "PASSWORD=hunter2 somecmd",
        ]}])
        result = _load(runbook_create(**data, overwrite=True, home=rb_home))
        assert "error" in result
        assert "PASSWORD" in result["error"]
        assert "hunter2" not in result["error"]

    def test_vault_placeholder_still_allowed(self, rb_home):
        data = _incident(steps=[{"id": "s1", "title": "x", "commands": [
            "curl -u admin:<secret:ansible/pass> http://localhost/health",
        ]}])
        result = _load(runbook_create(**data, overwrite=True, home=rb_home))
        assert result.get("status") in ("created", "updated"), result
