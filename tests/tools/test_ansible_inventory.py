"""YAPL P2（批次五十三）：ansible inventory 契约化守卫验收测试。

覆盖（任务书 §7）：正常（inventory 主机 ⊆ 拓扑表 → 放行）/ 绕行（含拓扑表外
主机 → 拒绝并列出）/ 混合（部分在部分不在 → 拒绝）/ 解析失败 fail-closed
（文件读不到 / 格式不识别 / 拓扑表读不到）。ini 与 yaml 两种 inventory 格式。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools.ansible_inventory_guard import (
    _extract_inventory_arg,
    _is_ansible_command,
    check_ansible_inventory_guard,
    parse_inventory,
)

TOPOLOGY = {
    "version": 4,
    "environments": [{"name": "prod", "isolation": "strict", "role": "prod"}],
    "clusters": [{"name": "k3s-prod", "env": "prod", "type": "k3s", "host_groups": ["k3s-node"]}],
    "hosts": [
        {"name": "node1", "env": "prod", "cluster": "k3s-prod", "endpoint": "10.0.0.1"},
        {"name": "node2", "env": "prod", "cluster": "k3s-prod", "endpoint": "10.0.0.2"},
    ],
}


@pytest.fixture
def inv_home(tmp_path, monkeypatch):
    home = tmp_path / "vigil_home"
    (home / "runbooks").mkdir(parents=True)
    (home / "topology.yaml").write_text(
        yaml.safe_dump(TOPOLOGY, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setenv("VIGIL_HOME", str(home))
    return home


def _write_inventory(home: Path, body: str, name: str = "inventory.ini") -> Path:
    path = home / name
    path.write_text(body, encoding="utf-8")
    return path


class TestParseInventory:
    def test_ini_format(self):
        hosts = parse_inventory(
            "[k3s]\nnode1 ansible_host=10.0.0.1\nnode2\n10.0.0.2\n"
            "[k3s:vars]\nansible_connection=ssh\n"
        )
        assert hosts == {"node1", "10.0.0.1", "node2", "10.0.0.2"}

    def test_yaml_format(self):
        hosts = parse_inventory(
            "all:\n"
            "  hosts:\n"
            "    node1:\n"
            "      ansible_host: 10.0.0.1\n"
            "    node2: {}\n"
            "  children:\n"
            "    prod:\n"
            "      hosts:\n"
            "        10.0.0.2: {}\n"
        )
        assert hosts == {"node1", "node2", "10.0.0.2"}

    def test_unrecognized_format_returns_none(self):
        assert parse_inventory("...") is None
        assert parse_inventory("") is None
        assert parse_inventory("!!!\n@@@\n") is None


class TestCommandDetection:
    def test_is_ansible_command(self):
        assert _is_ansible_command("ansible-playbook -i inv.yml site.yml")
        assert _is_ansible_command("ansible -i inv.yml all -m ping")
        assert _is_ansible_command("sudo ansible-playbook -i inv.yml site.yml")
        assert not _is_ansible_command("kubectl get pods")
        assert not _is_ansible_command("echo ansible-playbook")

    def test_extract_inventory_arg(self):
        assert _extract_inventory_arg("ansible-playbook -i inv.yml site.yml") == "inv.yml"
        assert _extract_inventory_arg("ansible -iinv.yml all -m ping") == "inv.yml"
        assert _extract_inventory_arg("ansible --inventory-file=inv.yml all -m ping") == "inv.yml"
        assert _extract_inventory_arg("ansible all -m ping") is None


class TestInventoryGuard:
    def test_all_hosts_in_topology_passes(self, inv_home):
        p = _write_inventory(inv_home, "[k3s]\nnode1\nnode2\n")
        assert check_ansible_inventory_guard(
            f"ansible-playbook -i {p} site.yml", home=inv_home) is None

    def test_endpoint_match_passes(self, inv_home):
        p = _write_inventory(inv_home, "[k3s]\n10.0.0.1\n10.0.0.2\n")
        assert check_ansible_inventory_guard(
            f"ansible -i {p} all -m ping", home=inv_home) is None

    def test_rogue_host_blocked(self, inv_home):
        p = _write_inventory(inv_home, "[k3s]\nnode1\nrogue-host\n")
        err = check_ansible_inventory_guard(
            f"ansible-playbook -i {p} site.yml", home=inv_home)
        assert err and "rogue-host" in err and "拓扑表外主机" in err
        assert "node1" not in err.split(":")[1]

    def test_mixed_hosts_blocked(self, inv_home):
        p = _write_inventory(inv_home, "[k3s]\nnode1\n10.0.0.1\n192.0.2.99\n")
        err = check_ansible_inventory_guard(
            f"ansible -i {p} all -m ping", home=inv_home)
        assert err and "192.0.2.99" in err

    def test_unreadable_inventory_fail_closed(self, inv_home):
        err = check_ansible_inventory_guard(
            "ansible-playbook -i /nonexistent/inv.yml site.yml", home=inv_home)
        assert err and "fail-closed" in err

    def test_unrecognized_format_fail_closed(self, inv_home):
        p = _write_inventory(inv_home, "!!!\n@@@\n", "bad.txt")
        err = check_ansible_inventory_guard(
            f"ansible-playbook -i {p} site.yml", home=inv_home)
        assert err and "格式无法识别" in err

    def test_garbage_tokens_treated_as_hosts_blocked(self, inv_home):
        # 无章节的垃圾行会当主机解析 → 不在拓扑表 → 同样 fail-closed 拒绝
        p = _write_inventory(inv_home, "this is not an inventory at all\n", "garbage.txt")
        err = check_ansible_inventory_guard(
            f"ansible-playbook -i {p} site.yml", home=inv_home)
        assert err and "拓扑表外主机" in err

    def test_missing_topology_fail_closed(self, tmp_path, monkeypatch):
        home = tmp_path / "empty_home"
        (home / "runbooks").mkdir(parents=True)
        monkeypatch.setenv("VIGIL_HOME", str(home))
        p = _write_inventory(home, "[k3s]\nnode1\n")
        err = check_ansible_inventory_guard(
            f"ansible-playbook -i {p} site.yml", home=home)
        assert err and "拓扑表" in err and "fail-closed" in err

    def test_no_inventory_arg_not_intercepted(self, inv_home):
        # P2 过渡期只拦显式 -i（OPS-DELTA #68 注明）；默认 inventory 留 P5 分类层。
        assert check_ansible_inventory_guard(
            "ansible all -m ping", home=inv_home) is None

    def test_non_ansible_command_passes(self, inv_home):
        assert check_ansible_inventory_guard(
            "kubectl get pods -n kube-system", home=inv_home) is None

    def test_guard_wired_into_check_all_command_guards(self, inv_home):
        """approval.check_all_command_guards 硬拦（先于 yolo/allowlist）。"""
        from tools.approval import check_all_command_guards
        p = _write_inventory(inv_home, "[k3s]\nnode1\nrogue-host\n")
        decision = check_all_command_guards(
            f"ansible-playbook -i {p} site.yml", env_type="local")
        assert decision.get("approved") is False
        assert decision.get("ansible_inventory") is True
        assert "rogue-host" in (decision.get("message") or "")
