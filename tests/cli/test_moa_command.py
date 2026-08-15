import queue
from unittest.mock import patch

from cli import HermesCLI
from hermes_cli.moa_config import decode_moa_turn


def _make_cli():
    cli = HermesCLI.__new__(HermesCLI)
    cli.config = {
        "moa": {
            "default_preset": "default",
            "presets": {
                "default": {
                    "reference_models": [{"provider": "openai-codex", "model": "gpt-5.5"}],
                    "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
                },
                "review": {
                    "reference_models": [{"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"}],
                    "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
                },
            },
        }
    }
    cli._pending_input = queue.Queue()
    cli._pending_agent_seed = None
    cli._pending_moa_config = None
    cli._pending_moa_disable_after_turn = False
    cli._pending_moa_restore_model = None
    cli._agent_running = False
    cli.agent = None
    cli.provider = "openrouter"
    cli.requested_provider = "openrouter"
    cli.model = "anthropic/claude-opus-4.8"
    cli.api_key = "test-key"
    cli.base_url = "https://openrouter.ai/api/v1"
    cli.api_mode = "chat_completions"
    return cli


def test_moa_command_removed_from_registry():
    # 批次二十六：/moa 已从 COMMAND_REGISTRY 删除（依赖 Nous 多模型，Vigil
    # 单模型死功能）。Model picker 的 MoA preset 与 `-m moa:<preset>` 路由保留。
    from hermes_cli.commands import resolve_command

    assert resolve_command("moa") is None


def test_moa_bare_is_unknown_command_no_switch():
    # /moa 删除后 process_command 走 unknown-command 路径：不切 provider、
    # 不排程 one-shot，也不设置 MoA restore。
    cli = _make_cli()
    cli._pending_moa_disable_after_turn = False
    with patch("cli._cprint"):
        assert cli.process_command("/moa") is True
    assert cli.provider != "moa"
    assert cli._pending_agent_seed is None
    assert cli._pending_moa_disable_after_turn is False


def test_moa_arg_is_not_queued_as_one_shot():
    # /moa <prompt> 不再是命令：参数不会被排程为 MoA one-shot prompt。
    cli = _make_cli()
    with patch("cli._cprint"):
        cli.process_command("/moa review")
    assert cli._pending_agent_seed is None
    assert cli._pending_moa_disable_after_turn is False
    assert cli.provider != "moa"
    assert cli.model != "default"




class TestNormalizeMoaModel:
    """#56828: `-Q -m moa:<preset>` must route through the MoA virtual provider.

    ``_normalize_moa_model`` maps the model string to (provider, preset); the
    __init__ wiring then forces ``requested_provider="moa"`` so the existing
    resolve_runtime_provider / agent_init MoA path runs in non-interactive mode.
    """

    def test_moa_prefix_maps_to_provider_and_preset(self):
        from cli import _normalize_moa_model
        assert _normalize_moa_model("moa:strategy") == ("moa", "strategy")




    def test_none_model_unchanged(self):
        from cli import _normalize_moa_model
        assert _normalize_moa_model(None) == (None, None)

    def test_colon_model_that_is_not_moa_unchanged(self):
        from cli import _normalize_moa_model
        # A provider:model form for a real provider must not be hijacked.
        assert _normalize_moa_model("openrouter:deepseek/deepseek-v4") == (
            None,
            "openrouter:deepseek/deepseek-v4",
        )

    def test_override_wins_over_explicit_provider(self):
        # __init__ resolves requested_provider as
        # ``_moa_provider_override or provider or ...``, so a moa: prefix must
        # take precedence over an explicit --provider (the #56828 deepseek case
        # where MoA was silently ignored).
        from cli import _normalize_moa_model
        override, model = _normalize_moa_model("moa:strategy")
        requested_provider = override or "deepseek" or "auto"
        assert requested_provider == "moa"
        assert model == "strategy"
