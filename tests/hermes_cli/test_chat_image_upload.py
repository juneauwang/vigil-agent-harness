"""task36 PART A —— 聊天图片上传端点（/api/chat/images）。

分层：只打 chat_api 新增上传端点（agent stub，不触发 LLM）。
硬约束断言：落盘在 VIGIL_HOME 专用目录且 0600；返回的引用**真的**能被
``agent.image_routing.extract_image_refs()`` 抽出来（实测，不是"返回了字符串"）；
超尺寸 → 明确 4xx；类型以嗅探为准（伪装 Content-Type 不算）；
客户端 filename 绝不参与路径（不越界）。
"""

from __future__ import annotations

import io
import os
import stat
from pathlib import Path

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

import hermes_cli.chat_api as chat_api
import hermes_cli.config as hc
from hermes_cli import web_server


def _png(width: int = 8, height: int = 8, color=(200, 30, 30)) -> bytes:
    """合成 PNG（纯色），不读任何真实图片。"""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _noisy_png(width: int = 96, height: int = 96) -> bytes:
    """噪声图（不可压缩）——用于稳定构造"超尺寸"的合成样本。"""
    from PIL import Image

    buf = io.BytesIO()
    Image.frombytes("RGB", (width, height), os.urandom(width * height * 3)).save(
        buf, format="PNG"
    )
    data = buf.getvalue()
    assert len(data) > 1024, f"noisy png too small: {len(data)}"
    return data


@pytest.fixture()
def client(monkeypatch):
    previous = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    chat_api.clear_chat_sessions()
    try:
        yield test_client
    finally:
        chat_api.clear_chat_sessions()
        if previous is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous


@pytest.fixture(autouse=True)
def env_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()


def _upload(client, data: bytes, filename: str, content_type: str = "image/png"):
    return client.post(
        "/api/chat/images",
        files={"file": (filename, data, content_type)},
    )


def _image_dir(home: Path) -> Path:
    return home / "uploads" / "chat_images"


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------

def test_upload_png_lands_0600_and_ref_is_extractable(client, env_home):
    """正常 PNG → 201 + 落盘 0600 + 返回引用可被 extract_image_refs 抽出。"""
    from agent.image_routing import extract_image_refs

    resp = _upload(client, _png(), "shot.png")
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    stored = Path(data["path"])

    # 落盘 + 权限 0600 + 目录 0700
    assert stored.is_file()
    assert stored.parent == _image_dir(env_home)
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert stat.S_IMODE(stored.parent.stat().st_mode) == 0o700
    assert data["mime"] == "image/png"

    # 引用形态实测：真的能被 extract_image_refs 解析（不是"返回了字符串"）。
    paths, urls = extract_image_refs(f"看下这张图 {data['path']}")
    assert paths == [data["path"]], (paths, data["path"])
    assert urls == []


def test_extension_follows_sniffed_mime_not_filename(client, env_home):
    """PNG 内容 + .txt 文件名 → 仍按嗅探存成 .png（引用才抽得出来）。"""
    from agent.image_routing import extract_image_refs

    resp = _upload(client, _png(), "actually_a_text.txt", content_type="text/plain")
    assert resp.status_code == 201, resp.text
    path = resp.json()["data"]["path"]
    assert path.endswith(".png")
    assert Path(path).read_bytes().startswith(b"\x89PNG")
    assert extract_image_refs(path)[0] == [path]


def test_filename_is_echoed_but_never_used_for_path(client, env_home):
    resp = _upload(client, _png(), "my screenshot.PNG")
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["original_name"] == "my screenshot.PNG"  # 回显（安全 basename）
    assert Path(data["path"]).name != "my screenshot.PNG"  # 落盘名不是客户端名


# ---------------------------------------------------------------------------
# 限制 / 拒绝
# ---------------------------------------------------------------------------

def test_oversize_rejected_with_readable_reason(client, env_home):
    (env_home / "config.yaml").write_text(
        "dashboard:\n  chat_image:\n    max_bytes: 1024\n", encoding="utf-8"
    )
    hc._LOAD_CONFIG_CACHE.clear()
    resp = _upload(client, _noisy_png(), "big.png")  # 不可压缩 → > 1 KiB
    assert resp.status_code == 413
    body = resp.json()
    assert body["error"]["code"] == "image_too_large"
    assert "上限" in body["error"]["message"]
    assert not list(_image_dir(env_home).glob("*.png")), "超限不得留下任何文件"


def test_non_image_rejected_even_with_image_content_type(client, env_home):
    """text 内容 + Content-Type: image/png → 拒绝（以嗅探为准）。"""
    resp = _upload(client, b"this is not an image at all", "fake.png", "image/png")
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_image"
    assert not list(_image_dir(env_home).glob("*"))


def test_empty_upload_rejected(client, env_home):
    resp = _upload(client, b"", "empty.png")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "empty_upload"


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("evil", ["../../evil.png", "/etc/evil.png", "..\\..\\evil.png"])
def test_traversal_filename_cannot_escape_upload_dir(client, env_home, evil):
    resp = _upload(client, _png(), evil)
    assert resp.status_code == 201, resp.text
    stored = Path(resp.json()["data"]["path"]).resolve()
    root = _image_dir(env_home).resolve()
    assert stored.parent == root, f"escaped upload dir: {stored}"
    # VIGIL_HOME 之外没有多出任何文件
    assert not (env_home.parent / "evil.png").exists()
    assert not Path("/etc/evil.png").exists()


def test_upload_requires_session_token(client, env_home):
    client.headers.pop(web_server._SESSION_HEADER_NAME, None)
    resp = _upload(client, _png(), "x.png")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 消息组装：图片引用 → native content parts（task36 PART A 后端接线）
# ---------------------------------------------------------------------------

class _StubAgent:
    """可脚本化 agent stub（照 test_batch31_chat_api 模式，不触发真 LLM）。"""

    _session_db = None
    provider = "custom"
    model = "vision-test"
    requested_provider = "custom"

    def __init__(self):
        self.calls = []

    def run_conversation(self, message, **kwargs):
        self.calls.append((message, kwargs))
        return {"final_response": "ok", "api_calls": 1, "completed": True,
                "partial": False}

    def interrupt(self, **kwargs):  # pragma: no cover - not exercised here
        return None


def _install_stub(monkeypatch) -> _StubAgent:
    agent = _StubAgent()
    monkeypatch.setattr(
        chat_api, "_create_chat_agent",
        lambda sid, model=None, provider=None: agent,
    )
    return agent


def _new_session(client) -> str:
    resp = client.post("/api/chat/sessions")
    assert resp.status_code == 200, resp.text
    return resp.json()["chat_session_id"]


def _send(client, sid: str, message: str) -> None:
    with client.stream(
        "POST", f"/api/chat/sessions/{sid}/messages", json={"message": message}
    ) as resp:
        assert resp.status_code == 200, resp.text
        for _ in resp.iter_lines():
            pass


def _native_config(home: Path) -> None:
    """强制 native 模式（决定表读 agent.image_input_mode）。"""
    (home / "config.yaml").write_text(
        "agent:\n  image_input_mode: native\n", encoding="utf-8"
    )
    hc._LOAD_CONFIG_CACHE.clear()


def test_plain_text_message_is_byte_identical(client, env_home, monkeypatch):
    """无图发送：交给 agent 的 user_message 与输入逐字节一致（行为不变）。"""
    agent = _install_stub(monkeypatch)
    sid = _new_session(client)
    _send(client, sid, "看下拓扑")
    message, _kwargs = agent.calls[0]
    assert message == "看下拓扑"
    assert isinstance(message, str)


def test_text_mode_with_image_path_stays_text(client, env_home, monkeypatch):
    """默认（text 模式）：路径在文本里，不硬塞 content parts。"""
    agent = _install_stub(monkeypatch)
    sid = _new_session(client)
    resp = _upload(client, _png(), "shot.png")
    path = resp.json()["data"]["path"]
    _send(client, sid, f"这是什么？{path}")
    message, _kwargs = agent.calls[0]
    assert isinstance(message, str) and path in message


def test_native_mode_attaches_image_url_content_part(client, env_home, monkeypatch):
    """native 模式：user_message 变成含 image_url 的 content parts（实测结构）。"""
    _native_config(env_home)
    agent = _install_stub(monkeypatch)
    sid = _new_session(client)
    path = _upload(client, _png(), "shot.png").json()["data"]["path"]

    _send(client, sid, f"这是什么？{path}")

    message, _kwargs = agent.calls[0]
    assert isinstance(message, list), f"expected content parts, got {type(message)}"
    kinds = [p.get("type") for p in message if isinstance(p, dict)]
    assert "text" in kinds and "image_url" in kinds
    img = next(p for p in message if p.get("type") == "image_url")
    url = img["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    text_part = next(p for p in message if p.get("type") == "text")
    assert path in text_part["text"]  # 路径提示仍在文本 part 里


# ---------------------------------------------------------------------------
# 会话标题不泄漏落盘路径（task36 补丁：真机实测发现标题变成
# "图里是什么颜色？ /tmp/…/uploads/chat_images/ab12.png"）
# ---------------------------------------------------------------------------


def _real_image(env_home: Path, name: str = "probe.png") -> str:
    """在临时 VIGIL_HOME 下落一张真图——extract_image_refs 认的是"磁盘上真实
    存在"的路径，所以标题剥离也只能用真文件验证。"""
    target = env_home / "uploads" / "chat_images" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_png())
    return str(target)


def test_title_text_strips_upload_path(env_home):
    """带图消息的标题只留用户打的字。"""
    path = _real_image(env_home)
    assert chat_api._title_text(f"图里是什么颜色？\n{path}", 60) == "图里是什么颜色？"


def test_title_text_plain_message_unchanged(env_home):
    """无图片引用时与 _preview 逐字节一致（纯文本路径不变）。"""
    assert chat_api._title_text("看下拓扑", 60) == chat_api._preview("看下拓扑", 60)
    assert chat_api._title_text("看下拓扑", 60) == "看下拓扑"


def test_title_text_image_only_falls_back_to_raw(env_home):
    """纯图消息（剥完为空）退回原文，不产出空标题。"""
    path = _real_image(env_home, "only.png")
    assert chat_api._title_text(path, 60) == chat_api._preview(path, 60)
    assert chat_api._title_text(path, 60) != ""


def test_session_listing_title_has_no_upload_path(client, monkeypatch):
    """端到端：发一条带图消息 → 会话列表标题里没有落盘路径。"""
    _install_stub(monkeypatch)
    sid = _new_session(client)
    path = _upload(client, _png(), "shot.png").json()["data"]["path"]
    _send(client, sid, f"图里是什么颜色？\n{path}")

    listing = client.get("/api/chat/sessions").json()
    titles = [s.get("last_message_preview") or "" for s in listing.get("sessions", [])]
    title = next(t for t in titles if t)
    assert path not in title, title
    assert "图里是什么颜色？" in title
