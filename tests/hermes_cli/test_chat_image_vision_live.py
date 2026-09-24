"""task36 PART C —— 真机 E2E：上传的图**真的**进了 vision 模型吗。

前面的单测只证明"链路通了"（端点落盘、消息组装成 ``image_url`` content
parts），**不证明模型收到了图**。这个文件跑一轮真实对话来关掉那个缺口：
在临时 VIGIL_HOME 下起 dashboard，上传一张内容可验证的合成图（蓝色方块 +
橙色圆，纯色无文字），问模型"有几个图形、什么形状、什么颜色"，断言回答**说中
图里的内容**。图里没有文字、没有文件名线索，颜色/形状/数量都不是能从上下文
猜出来的 —— 说中了才说明像素真的到了模型。

opt-in，不进默认 CI：

    VIGIL_LIVE_TESTS=1 pytest tests/hermes_cli/test_chat_image_vision_live.py -v

凭据只从**环境变量**取（刻意不读 ~/.vigil/.env）：``DEEPSEEK_API_KEY`` 或
``OPENROUTER_API_KEY``。
"""

from __future__ import annotations

import io
import os

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

import hermes_cli.chat_api as chat_api
import hermes_cli.config as hc
from hermes_cli import web_server

LIVE = os.environ.get("VIGIL_LIVE_TESTS") == "1"

# 注意：tests/conftest.py 的 hermetic 不变量会在**每个测试前 unset 所有凭据型
# env var**（"local developer keys cannot leak in"）。所以这里在**收集期**先把
# key 抓下来存成模块常量，再由 fixture 针对本测试 setenv 回去 —— 只在 opt-in
# 的 live 测试里放行，且 key 只来自进程环境（刻意不读 ~/.vigil/.env）。

DS_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")

if DS_KEY:
    _PROVIDER, _MODEL = "deepseek", "deepseek-flash"
elif OR_KEY:
    _PROVIDER, _MODEL = "openrouter", "google/gemini-2.5-flash"
else:
    _PROVIDER, _MODEL = "", ""

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not LIVE, reason="live-only — set VIGIL_LIVE_TESTS=1"),
    pytest.mark.skipif(not _PROVIDER, reason="no DEEPSEEK_API_KEY / OPENROUTER_API_KEY"),
]


def _probe_png() -> bytes:
    """合成探针图：左上蓝色实心方块 + 右下橙色实心圆（无文字，不可猜）。"""
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (400, 300), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rectangle([40, 40, 160, 160], fill=(0, 60, 220))
    d.ellipse([230, 140, 360, 270], fill=(255, 140, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _mentions(text: str, *needles: str) -> bool:
    low = text.lower()
    return any(n.lower() in low for n in needles)


@pytest.fixture()
def live_client(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        f"model:\n  default: {_MODEL}\n  provider: {_PROVIDER}\n",
        encoding="utf-8",
    )
    hc._LOAD_CONFIG_CACHE.clear()
    # 见 LIVE 上方注释：把收集期抓到的 key 放回本轮测试的进程环境。
    monkeypatch.setenv("DEEPSEEK_API_KEY", DS_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
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
        hc._LOAD_CONFIG_CACHE.clear()


def test_real_vision_model_sees_uploaded_chat_image(live_client):
    """真实一轮：上传 → 发送 → 模型回答必须说中图里的形状与颜色。"""
    sid = live_client.post("/api/chat/sessions").json()["chat_session_id"]
    up = live_client.post(
        "/api/chat/images",
        files={"file": ("vision_probe.png", _probe_png(), "image/png")},
    )
    assert up.status_code == 201, up.text
    path = up.json()["data"]["path"]

    question = "这张图里有几个图形？分别是什么形状、什么颜色？"
    events = []
    with live_client.stream(
        "POST", f"/api/chat/sessions/{sid}/messages", json={"message": f"{question}\n{path}"}
    ) as resp:
        assert resp.status_code == 200, resp.text
        for line in resp.iter_lines():
            if line.startswith("data:"):
                import json as _json

                try:
                    events.append(_json.loads(line[5:].strip()))
                except ValueError:
                    continue

    done = [e for e in events if e.get("type") == "chat:done"]
    assert done, f"no chat:done event; types={[e.get('type') for e in events]}"
    assert done[-1].get("completed") and not done[-1].get("failed"), done[-1]
    answer = done[-1].get("final_response") or ""

    # 说中图里的内容 = 像素真的到了模型（文本上下文里没有任何颜色/形状线索）。
    assert _mentions(answer, "蓝", "blue"), f"没提到蓝色方块：{answer!r}"
    assert _mentions(answer, "橙", "orange"), f"没提到橙色圆：{answer!r}"
    assert _mentions(answer, "方", "square"), f"没提到方形：{answer!r}"
    assert _mentions(answer, "圆", "circle"), f"没提到圆形：{answer!r}"
