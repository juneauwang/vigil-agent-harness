from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = REPO_ROOT / "setup-vigil.sh"


def test_setup_vigil_script_is_valid_shell():
    result = subprocess.run(["bash", "-n", str(SETUP_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_setup_vigil_script_covers_clone_install_init():
    content = SETUP_SCRIPT.read_text(encoding="utf-8")

    assert "git clone" in content
    assert "vigil ops-init" in content
    assert "sync --extra all --locked" in content
    assert "pip install -e ." in content
    assert "is_termux()" in content
    assert ".[termux]" in content
    assert "vigil" in content
