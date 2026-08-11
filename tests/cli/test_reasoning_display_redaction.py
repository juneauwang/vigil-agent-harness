"""Reasoning/thinking display must redact credentials before rendering.

OPS-DELTA #5: reasoning deltas stream straight from the provider to the CLI
display with no redaction — a model that repeats a credential value in its
thinking block (e.g. a value it saw in tool output) would otherwise print it
verbatim on screen. Covers the three render surfaces: the live streaming box
(_stream_reasoning_delta), the buffered [thinking] preview
(_emit_reasoning_preview), and the box-close tail flush.
"""
import pytest


def _make_streaming_cli(monkeypatch):
    """Minimal HermesCLI with real _stream_reasoning_delta / _emit_reasoning_preview."""
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.show_reasoning = True
    cli.streaming_enabled = True
    cli.verbose = False
    cli._stream_box_opened = False
    cli._reasoning_box_opened = False
    cli._reasoning_shown_this_turn = False
    cli._reasoning_buf = ""
    cli._reasoning_preview_buf = ""
    cli._deferred_content = ""
    cli._scrollback_box_width = lambda: 80
    cli._printed = []

    def _capture(text):
        cli._printed.append(text)

    monkeypatch.setattr("cli._cprint", _capture)
    return cli


def _printed_joined(cli):
    return "".join(cli._printed)


class TestStreamingReasoningBoxRedaction:
    def test_sshpass_assignment_masked_even_split_across_deltas(self, monkeypatch):
        """Secret split across two stream deltas must still be masked — the
        line is reassembled in the buffer before redaction runs."""
        cli = _make_streaming_cli(monkeypatch)
        cli._stream_reasoning_delta("the vault pass is SSHPASS='fake-")
        cli._stream_reasoning_delta("pass-123' sshpass -e ssh user@203.0.113.10\n")
        out = _printed_joined(cli)
        assert "fake-pass-123" not in out
        assert "SSHPASS='***'" in out
        assert "sshpass -e ssh user@203.0.113.10" in out

    def test_json_ssh_key_field_masked(self, monkeypatch):
        cli = _make_streaming_cli(monkeypatch)
        cli._stream_reasoning_delta('bao returned {"ssh_key": "fake-secret-xyz"}\n')
        out = _printed_joined(cli)
        assert "fake-secret-xyz" not in out
        assert '"ssh_key": "***"' in out

    def test_box_close_flushes_masked_tail(self, monkeypatch):
        """A trailing partial line (no newline yet) must be redacted when the
        reasoning box is closed."""
        cli = _make_streaming_cli(monkeypatch)
        cli._stream_reasoning_delta("hold on, key is SSHPASS='fake-pass-123'")
        assert "fake-pass-123" not in _printed_joined(cli)  # buffered, not yet shown
        cli._close_reasoning_box()
        out = _printed_joined(cli)
        assert "fake-pass-123" not in out
        assert "SSHPASS='***'" in out


class TestReasoningPreviewRedaction:
    def test_preview_masks_credential_values(self, monkeypatch):
        """Verbose buffered preview path must redact before formatting."""
        cli = _make_streaming_cli(monkeypatch)
        cli._emit_reasoning_preview('checked {"private_key": "fake-secret-xyz"}')
        out = _printed_joined(cli)
        assert "fake-secret-xyz" not in out
        assert '"private_key": "***"' in out

    def test_preview_masks_multiline_env_value(self, monkeypatch):
        cli = _make_streaming_cli(monkeypatch)
        cli._emit_reasoning_preview("the sudo pass is\nSUDO_PASS='fake-pass-123'\nnext")
        out = _printed_joined(cli)
        assert "fake-pass-123" not in out
        assert "SUDO_PASS='***'" in out
