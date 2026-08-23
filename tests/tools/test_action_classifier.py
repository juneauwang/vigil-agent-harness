"""YAPL P5 操作分类层（tools/action_classifier.py）——terminal 命令串 → 动作枚举。

覆盖：规则表大全（docker/kubectl/systemctl/apt/pm2/helm/ansible/ssh/scp/curl/
备份族 × 子命令 → 动作断言）、管道不干扰、链式取保守、unknown 兜底、sudo 前缀
剥离。动作词表 = schemas.yaml actions 23 个（unknown 是兜底不是动作枚举）。
"""

from __future__ import annotations

import pytest

from tools.action_classifier import classify_command, classify_single

# (命令, 期望动作, 期望规则 id)
RULES = [
    # docker compose（长模式先于通用 docker）
    ("docker compose up -d", "deploy", "docker.compose.up"),
    ("docker compose down", "decommission", "docker.compose.down"),
    ("docker compose restart web", "restart", "docker.compose.restart"),
    ("docker compose start web", "start", "docker.compose.start"),
    ("docker compose stop web", "stop", "docker.compose.stop"),
    ("docker compose rm -f web", "remove", "docker.compose.rm"),
    ("docker compose build web", "install", "docker.compose.build"),
    ("docker compose pull web", "install", "docker.compose.pull"),
    # docker 通用
    ("docker restart harbor", "restart", "docker.restart"),
    ("docker start harbor", "start", "docker.start"),
    ("docker stop harbor", "stop", "docker.stop"),
    ("docker reload nginx", "reload", "docker.reload"),
    ("docker ps", "query", "docker.query"),
    ("docker logs -f web", "fetch_log", "docker.logs"),
    ("docker inspect web", "query", "docker.query"),
    ("docker stats", "query", "docker.query"),
    ("docker port web 80", "query", "docker.query"),
    ("docker images", "query", "docker.query"),
    ("docker build -t app .", "install", "docker.build"),
    ("docker pull nginx:latest", "install", "docker.pull"),
    ("docker push app:1.0", "install", "docker.push"),
    ("docker rm stale-container", "remove", "docker.remove"),
    ("docker rmi old-image", "remove", "docker.remove"),
    # kubectl
    ("kubectl rollout restart deploy/app", "restart", "kubectl.rollout_restart"),
    ("kubectl rollout undo deploy/app", "rollback", "kubectl.rollout_undo"),
    ("kubectl set image deploy/app app=v2", "deploy", "kubectl.set_image"),
    ("kubectl apply -f deploy.yaml", "deploy", "kubectl.apply"),
    ("kubectl scale deploy/app --replicas=5", "scale", "kubectl.scale"),
    ("kubectl delete pod web-1", "decommission", "kubectl.delete"),
    ("kubectl get pods -n prod", "query", "kubectl.query"),
    ("kubectl describe deploy app", "query", "kubectl.query"),
    ("kubectl logs -f deploy/app", "fetch_log", "kubectl.logs"),
    ("kubectl top nodes", "query", "kubectl.query"),
    # systemctl / service
    ("systemctl restart myapp", "restart", "systemctl.restart"),
    ("systemctl start myapp", "start", "systemctl.start"),
    ("systemctl stop myapp", "stop", "systemctl.stop"),
    ("systemctl reload nginx", "reload", "systemctl.reload"),
    ("systemctl enable nginx", "enable", "systemctl.enable"),
    ("systemctl disable nginx", "disable", "systemctl.disable"),
    ("systemctl status nginx", "query", "systemctl.status"),
    ("systemctl reboot", "reboot", "systemctl.reboot"),
    ("systemctl poweroff", "shutdown", "systemctl.poweroff"),
    ("service nginx restart", "restart", "service.restart"),
    ("service nginx stop", "stop", "service.stop"),
    # 包管理
    ("apt install nginx", "install", "apt.install"),
    ("apt-get install -y nginx", "install", "apt.install"),
    ("apt remove nginx", "remove", "apt.remove"),
    ("apt purge nginx", "remove", "apt.remove"),
    ("apt upgrade", "upgrade", "apt.upgrade"),
    ("dnf install httpd", "install", "dnf.install"),
    ("yum remove httpd", "remove", "dnf.remove"),
    ("dnf upgrade", "upgrade", "dnf.upgrade"),
    ("pip install requests", "install", "pip.install"),
    # pm2
    ("pm2 restart app", "restart", "pm2.restart"),
    ("pm2 start app", "start", "pm2.start"),
    ("pm2 stop app", "stop", "pm2.stop"),
    # helm
    ("helm install app chart", "deploy", "helm.install"),
    ("helm upgrade app chart", "upgrade", "helm.upgrade"),
    ("helm uninstall app", "decommission", "helm.uninstall"),
    ("helm delete app", "decommission", "helm.uninstall"),
    ("helm rollback app 1", "rollback", "helm.rollback"),
    # ansible
    ("ansible-playbook site.yml", "deploy", "ansible.playbook"),
    ("ansible all -m ping", "deploy", "ansible.ad_hoc"),
    # 受控通道
    ("ssh root@203.0.113.10 'df -h'", "run_script", "ssh.controlled"),
    ("scp a.txt ops@host:/tmp", "transfer_file", "scp.transfer"),
    ("rsync -av /data ops@host:/backup", "transfer_file", "rsync.transfer"),
    ("sftp ops@host", "transfer_file", "sftp.transfer"),
    # curl/wget
    ("curl -s http://127.0.0.1:8080/health", "query", "curl.probe"),
    ("curl -o /tmp/file http://x/y", "transfer_file", "curl.download"),
    ("curl -O http://x/y.tar.gz", "transfer_file", "curl.download"),
    ("curl --output /tmp/f http://x", "transfer_file", "curl.download"),
    ("wget -qO- http://x/health", "query", "wget.probe"),
    ("wget -O /tmp/f http://x", "transfer_file", "wget.download"),
    # 备份/恢复族
    ("pg_dump -U postgres mydb > /tmp/db.sql", "backup", "backup.pg_dump"),
    ("mysqldump -u root orders > /tmp/o.sql", "backup", "backup.mysqldump"),
    ("pg_restore -d mydb /tmp/db.sql", "restore", "restore.pg_restore"),
    ("tar -czf /tmp/a.tar.gz /data", "backup", "backup.tar_create"),
    ("tar -xzf /tmp/a.tar.gz -C /data", "restore", "restore.tar_extract"),
    # 主机生命周期
    ("reboot", "reboot", "host.reboot"),
    ("shutdown -r now", "shutdown", "host.shutdown"),
]


@pytest.mark.parametrize("cmd,action,rule", RULES)
def test_rules_table(cmd, action, rule):
    result = classify_command(cmd)
    assert result["action"] == action, (cmd, result)
    assert result["rule"] == rule, (cmd, result)
    assert result["chain"] is None, cmd


def test_sudo_prefix_stripped():
    """sudo/doas 前缀剥离后按真实命令分类（sudo 路径与 terminal 同矩阵）。"""
    assert classify_command("sudo systemctl restart nginx")["action"] == "restart"
    assert classify_command("sudo -u root systemctl restart nginx")["action"] == "restart"
    assert classify_command("sudo -i docker ps")["action"] == "query"
    assert classify_command("doas systemctl status nginx")["action"] == "query"


def test_pipe_does_not_interfere():
    """词面绕过消失：管道按主命令分类（| grep 只影响成功标准）。"""
    assert classify_command("docker ps | grep harbor")["action"] == "query"
    assert classify_command("docker ps --format '{{.Names}}' | grep harbor")["action"] == "query"
    assert classify_command("kubectl get pods | grep running")["action"] == "query"
    # 主命令是变更类 → 管道不把动作降成 query
    assert classify_command("systemctl restart nginx | tee /tmp/log")["action"] == "restart"


def test_chain_takes_conservative_action():
    """链式取保守：&&/;/|| 拆子命令，action = 静态档位最保守；chain 携带全部。"""
    result = classify_command("docker ps && systemctl restart nginx")
    assert result["chain"] is not None
    assert [c["action"] for c in result["chain"]] == ["query", "restart"]
    assert result["action"] == "restart"  # 静态保守序：restart > query

    result = classify_command("systemctl restart nginx; rm -rf /data")
    assert [c["action"] for c in result["chain"]] == ["restart", "unknown"]
    assert result["action"] == "unknown"  # unknown 最保守

    result = classify_command("kubectl get pods || kubectl delete pod x")
    assert result["action"] == "decommission"
    assert [c["action"] for c in result["chain"]] == ["query", "decommission"]


def test_chain_segments_respect_pipes():
    """链式段内管道不干扰：每段取主命令分类。"""
    result = classify_command("docker ps | grep harbor && docker restart harbor")
    assert [c["action"] for c in result["chain"]] == ["query", "restart"]
    assert result["action"] == "restart"


def test_unknown_falls_back_conservative():
    """识别不出 → unknown（矩阵漏配 → 默认 approve，绝不 deny）。"""
    for cmd in ("mv a.txt b.txt", "cp x y", "frobnicate --all", "echo hello",
                "git push origin main", "ls -la", "cat /etc/hosts"):
        result = classify_command(cmd)
        assert result["action"] == "unknown", cmd
        assert "OPS-DELTA 登记新规则" in (result["note"] or ""), cmd


def test_empty_and_non_string():
    assert classify_command("")["action"] == "unknown"
    assert classify_command(None)["action"] == "unknown"
    assert classify_command("   ")["action"] == "unknown"


def test_curl_write_operation_is_unknown():
    """curl 写操作（-X POST / -d / -F）→ unknown 保守（不是 query）。"""
    for cmd in ("curl -X POST http://x/api", "curl -d 'a=1' http://x/api",
                "curl -F file=@f http://x/upload"):
        assert classify_command(cmd)["action"] == "unknown", cmd
