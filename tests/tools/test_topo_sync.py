"""拓扑自维护（任务18）—— tfstate 同步源 + 漂移报告 + 周期重扫验收。

覆盖：
  - parse_tfstate：alicloud/aws/azurerm 资源族映射、child_modules 递归、
    属性缺失（endpoint=None 不炸）、非计算资源忽略、坏 JSON → 空；
  - topo_drift_report：added / changed（逐字段 old→new）/ vanished（source=
    terraform 未出现）/ unchanged 计数；
  - apply_tfstate_rows：只追加新增行、同名既有行（manual 字段）绝不覆盖、
    幂等（二次 apply 零新增）；
  - CLI（hermes_cli.topo_sync.main）：报告落盘 runtime/topo_drift.json +
    --apply 写拓扑 + 缺文件退出码；
  - 周期重扫调度：register/unregister 幂等（cron marker job + no_agent 脚本）。

测试数据全部为占位 IP（203.0.113.x / 10.203.0.x）与伪造 tfstate，无真实云状态。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
import yaml

from tools.topo_discovery import (
    apply_tfstate_rows,
    parse_tfstate,
    topo_drift_report,
    write_drift_report,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def shome(tmp_path, monkeypatch):
    """隔离 VIGIL_HOME 的空拓扑 home（drift 全 added 起步）。"""
    home = tmp_path / "vigil_home"
    home.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(home))
    (home / "topology.yaml").write_text(yaml.safe_dump({
        "version": 4,
        "updated_at": "2026-09-06",
        "environments": [],
        "hosts": [],
        "clusters": [],
    }), encoding="utf-8")
    return home


def _tfstate(*resources: dict) -> str:
    return json.dumps({
        "format_version": "1.2",
        "values": {"root_module": {"resources": list(resources)}},
    })


def _aws(name="web-prod", public="203.0.113.10", private="10.203.0.10",
         env=None, tags_name=None):
    values = {"instance_type": "t3.micro", "private_ip": private}
    if public is not None:
        values["public_ip"] = public
    tags = {}
    if tags_name is not None:
        tags["Name"] = tags_name
    if env is not None:
        tags["env"] = env
    if tags:
        values["tags"] = tags
    return {"mode": "managed", "type": "aws_instance", "name": name,
            "address": f"aws_instance.{name}", "values": values}


# ---------------------------------------------------------------------------
# parse_tfstate
# ---------------------------------------------------------------------------

class TestParseTfstate:
    def test_aws_instance_full_mapping(self):
        rows = parse_tfstate(_tfstate(_aws(env="prod", tags_name="web-prod")))
        assert len(rows) == 1
        row = rows[0]
        assert row["name"] == "web-prod"  # tags.Name 优先于资源名
        assert row["endpoint"] == "203.0.113.10"  # 公网优先
        assert row["public_ip"] == "203.0.113.10"
        assert row["private_ip"] == "10.203.0.10"
        assert row["env"] == "prod"  # env tag 约定
        assert row["source"] == "terraform"
        assert row["managed_by"] == "terraform"
        assert row["needs_review"] is True
        assert row["type"] == "host"

    def test_alicloud_instance_name_and_private_only(self):
        res = {"mode": "managed", "type": "alicloud_instance", "name": "bastion",
               "address": "alicloud_instance.bastion",
               "values": {"instance_name": "bastion-1",
                          "private_ip": "10.203.0.11"}}
        rows = parse_tfstate(_tfstate(res))
        assert rows[0]["name"] == "bastion-1"  # alicloud 用 instance_name
        assert rows[0]["endpoint"] == "10.203.0.11"  # 无公网 → 私网兜底
        assert rows[0]["public_ip"] is None

    def test_azurerm_ip_attribute_names(self):
        res = {"mode": "managed", "type": "azurerm_virtual_machine", "name": "vm1",
               "address": "azurerm_virtual_machine.vm1",
               "values": {"name": "vm1", "public_ip_address": "203.0.113.11",
                          "private_ip_address": "10.203.0.12",
                          "tags": {"Environment": "prod"}}}
        rows = parse_tfstate(_tfstate(res))
        assert rows[0]["endpoint"] == "203.0.113.11"
        assert rows[0]["env"] == "prod"  # Environment tag 命中

    def test_child_modules_resources_discovered(self):
        payload = {
            "values": {"root_module": {
                "child_modules": [{
                    "resources": [_aws(name="nested", tags_name="nested-host")],
                }],
            }},
        }
        rows = parse_tfstate(json.dumps(payload))
        assert [r["name"] for r in rows] == ["nested-host"]

    def test_non_compute_resources_ignored(self):
        sg = {"mode": "managed", "type": "aws_security_group", "name": "sg",
              "address": "aws_security_group.sg", "values": {}}
        assert parse_tfstate(_tfstate(_aws(), sg))[0]["name"] != "sg"
        assert len(parse_tfstate(_tfstate(_aws(), sg))) == 1

    def test_missing_attrs_keep_row_with_none_endpoint(self):
        res = {"mode": "managed", "type": "aws_instance", "name": "bare",
               "address": "aws_instance.bare", "values": {}}
        rows = parse_tfstate(_tfstate(res))
        assert rows[0]["name"] == "bare"
        assert rows[0]["endpoint"] is None  # 缺属性不炸，交人工 review

    def test_bad_json_returns_empty(self):
        assert parse_tfstate("not json{") == []
        assert parse_tfstate("") == []


# ---------------------------------------------------------------------------
# drift + apply
# ---------------------------------------------------------------------------

class TestDriftAndApply:
    def _seed_manual_host(self, home: Path, name="web", endpoint="198.51.100.1"):
        topo = yaml.safe_load((home / "topology.yaml").read_text(encoding="utf-8"))
        topo["hosts"].append({"name": name, "type": "host", "env": "prod",
                              "endpoint": endpoint, "os": "Ubuntu 24.04",
                              "owner": "alice", "credentials": []})
        (home / "topology.yaml").write_text(
            yaml.safe_dump(topo, allow_unicode=True, sort_keys=False),
            encoding="utf-8")

    def test_empty_topology_all_added(self, shome):
        report = topo_drift_report(shome, parse_tfstate(_tfstate(_aws())))
        assert len(report["added"]) == 1
        assert report["vanished"] == [] and report["changed"] == []
        assert report["unchanged"] == 0

    def test_changed_fields_listed_not_applied(self, shome):
        self._seed_manual_host(shome, name="web-prod", endpoint="198.51.100.1")
        rows = parse_tfstate(_tfstate(_aws(env="prod")))  # 同名，endpoint 不同
        report = topo_drift_report(shome, rows)
        assert report["added"] == []
        assert len(report["changed"]) == 1
        fields = {c["field"]: (c["old"], c["new"]) for c in report["changed"][0]["changes"]}
        assert fields["endpoint"] == ("198.51.100.1", "203.0.113.10")
        # apply 对 changed 行不落盘（同名绝不覆盖）
        result = apply_tfstate_rows(shome, rows)
        assert result["applied"] == []
        topo = yaml.safe_load((shome / "topology.yaml").read_text(encoding="utf-8"))
        row = next(h for h in topo["hosts"] if h["name"] == "web-prod")
        assert row["endpoint"] == "198.51.100.1"  # 手动值保留
        assert row["owner"] == "alice"  # manual 字段完整

    def test_vanished_flags_terraform_rows_only(self, shome):
        # 拓扑里已有两行 terraform 行（上次同步）+ 一行 manual
        for name, endpoint in (("old-tf-1", "203.0.113.20"), ("old-tf-2", "203.0.113.21")):
            row = parse_tfstate(_tfstate(_aws(name=name, public=endpoint,
                                              tags_name=name)))[0]
            apply_tfstate_rows(shome, [row])
        self._seed_manual_host(shome, name="manual-host")
        # 本次 tfstate 只剩 old-tf-1
        report = topo_drift_report(shome, parse_tfstate(
            _tfstate(_aws(name="old-tf-1", public="203.0.113.20", tags_name="old-tf-1"))))
        assert report["vanished"] == [{"name": "old-tf-2",
                                       "endpoint": "203.0.113.21",
                                       "source": "terraform"}]
        # manual-host 不在 vanished（不是 terraform 来源，不受重扫管辖）
        assert all(v["name"] != "manual-host" for v in report["vanished"])

    def test_apply_writes_added_only_and_is_idempotent(self, shome):
        rows = parse_tfstate(_tfstate(_aws(env="prod", tags_name="web-prod")))
        result = apply_tfstate_rows(shome, rows)
        assert result["applied"] == ["web-prod"]
        topo = yaml.safe_load((shome / "topology.yaml").read_text(encoding="utf-8"))
        row = next(h for h in topo["hosts"] if h["name"] == "web-prod")
        assert row["needs_review"] is True and row["source"] == "terraform"
        assert topo["version"] == 4
        # 幂等：二次 apply 零新增、行数不变
        result2 = apply_tfstate_rows(shome, rows)
        assert result2["applied"] == []
        topo2 = yaml.safe_load((shome / "topology.yaml").read_text(encoding="utf-8"))
        assert len(topo2["hosts"]) == len(topo["hosts"])

    def test_v0_1_topology_rejected(self, shome):
        (shome / "topology.yaml").write_text(yaml.safe_dump(
            {"core_entities": [{"name": "x", "env": "local"}]}), encoding="utf-8")
        from tools.topo_discovery import DiscoveryError
        with pytest.raises(DiscoveryError):
            apply_tfstate_rows(shome, parse_tfstate(_tfstate(_aws())))

    def test_write_drift_report_lands_runtime(self, shome):
        report = topo_drift_report(shome, [])
        path = write_drift_report(shome, report)
        assert path == shome / "runtime" / "topo_drift.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["total_discovered"] == 0 and "ts" in payload


# ---------------------------------------------------------------------------
# CLI（hermes_cli.topo_sync.main）
# ---------------------------------------------------------------------------

class TestTopoSyncCli:
    def _write_state(self, home: Path) -> Path:
        state = home / "infra.tfstate"
        state.write_text(_tfstate(_aws(env="prod", tags_name="web-prod")),
                         encoding="utf-8")
        return state

    def test_report_only_default(self, shome, capsys):
        state = self._write_state(shome)
        from hermes_cli.topo_sync import main
        rc = main(["--tfstate", str(state), "--env", "prod"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "拓扑漂移报告" in out and "web-prod" in out
        assert "只报告不落盘" in out
        assert (shome / "runtime" / "topo_drift.json").is_file()
        # 未 --apply：拓扑仍是空 hosts
        topo = yaml.safe_load((shome / "topology.yaml").read_text(encoding="utf-8"))
        assert topo["hosts"] == []

    def test_apply_writes_topology(self, shome, capsys):
        state = self._write_state(shome)
        from hermes_cli.topo_sync import main
        rc = main(["--tfstate", str(state), "--env", "prod", "--apply", "--yes"])
        assert rc == 0
        topo = yaml.safe_load((shome / "topology.yaml").read_text(encoding="utf-8"))
        assert [h["name"] for h in topo["hosts"]] == ["web-prod"]

    def test_missing_tfstate_file_exit_2(self, shome, capsys):
        from hermes_cli.topo_sync import main
        rc = main(["--tfstate", str(shome / "nope.tfstate")])
        assert rc == 2

    def test_no_paths_exit_2(self, shome, capsys):
        from hermes_cli.topo_sync import main
        rc = main([])
        assert rc == 2


# ---------------------------------------------------------------------------
# 周期重扫调度（复用 cron 调度器；opt-in 默认关）
# ---------------------------------------------------------------------------

class TestRescanSchedule:
    def test_register_update_unregister_idempotent(self, shome, capsys):
        from cron.jobs import list_jobs, use_cron_store
        from hermes_cli.topo_sync import register_topo_rescan_schedule

        with use_cron_store(shome):
            assert list_jobs(include_disabled=True) == []
        assert register_topo_rescan_schedule("24h", shome) == "registered"
        with use_cron_store(shome):
            jobs = list_jobs(include_disabled=True)
        assert len(jobs) == 1
        job = jobs[0]
        assert job["name"] == "topo_sync"          # marker job（runbook_schedule 同款）
        assert job["no_agent"] is True             # 脚本即任务，无 LLM
        script = Path(job["script"])
        assert script.is_file() and script.stat().st_mode & 0o777 == 0o700
        script_text = script.read_text(encoding="utf-8")
        assert "-m hermes_cli.topo_sync" in script_text  # 只读报告路径
        assert f'VIGIL_HOME="{shome}"' in script_text    # profile 固定

        # 幂等更新
        assert register_topo_rescan_schedule("1d", shome) == "updated"
        with use_cron_store(shome):
            assert len(list_jobs(include_disabled=True)) == 1

        # off 注销 + 再注销 noop
        assert register_topo_rescan_schedule("off", shome) == "unregistered"
        with use_cron_store(shome):
            assert list_jobs(include_disabled=True) == []
        assert register_topo_rescan_schedule("off", shome) == "noop"

    def test_schedule_parser_flag_flows(self, shome, monkeypatch, capsys):
        """CLI --schedule 透传到注册器；off 走注销分支。"""
        monkeypatch.setattr("hermes_cli.topo_sync._active_home", lambda: shome)
        calls: list = []
        monkeypatch.setattr("hermes_cli.topo_sync.register_topo_rescan_schedule",
                            lambda interval, home=None: calls.append((interval, home)))
        from hermes_cli.topo_sync import main
        assert main(["--schedule", "24h"]) == 0
        assert calls and calls[0][0] == "24h"
