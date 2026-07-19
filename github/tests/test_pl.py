import importlib.util
import json
from pathlib import Path


HELPER = Path(__file__).resolve().parents[1] / "_lib" / "pl.py"
SPEC = importlib.util.spec_from_file_location("pluglayer_action_helper", HELPER)
pl = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(pl)


def _clear_env(monkeypatch):
    for key in (
        "PLUGLAYER_ENV_JSON",
        "PLUGLAYER_ENV_TEXT",
        "PLUGLAYER_ENV_FILE",
        "PLUGLAYER_ENV_FORMAT",
        "PLUGLAYER_MERGE",
        "PLUGLAYER_RESTART_MODE",
        "PLUGLAYER_REDEPLOY_STRATEGY",
    ):
        monkeypatch.delenv(key, raising=False)


def test_env_payload_supports_json_object(monkeypatch, capsys):
    _clear_env(monkeypatch)
    monkeypatch.setenv("PLUGLAYER_ENV_JSON", '{"PORT": 8000, "ENABLED": true}')

    assert pl.cmd_env_payload([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert json.loads(payload["content"]) == {"PORT": 8000, "ENABLED": True}
    assert payload["input_format"] == "json"
    assert payload["merge"] is True
    assert payload["restart_mode"] == "restart"


def test_env_payload_reads_regular_env_file_and_infers_format(monkeypatch, capsys, tmp_path):
    _clear_env(monkeypatch)
    env_file = tmp_path / "runtime.env"
    env_file.write_text('KEY_1=1xb\nKEY_2="xxx"\n', encoding="utf-8")
    monkeypatch.setenv("PLUGLAYER_ENV_FILE", str(env_file))
    monkeypatch.setenv("PLUGLAYER_REDEPLOY_STRATEGY", "recreate")

    assert pl.cmd_env_payload([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["content"] == 'KEY_1=1xb\nKEY_2="xxx"\n'
    assert payload["input_format"] == "dotenv"
    assert payload["redeploy_strategy"] == "recreate"


def test_env_payload_rejects_multiple_sources(monkeypatch, capsys):
    _clear_env(monkeypatch)
    monkeypatch.setenv("PLUGLAYER_ENV_JSON", '{"A": "1"}')
    monkeypatch.setenv("PLUGLAYER_ENV_TEXT", "B=2")

    assert pl.cmd_env_payload([]) == 1
    assert "only one" in capsys.readouterr().err


def test_env_payload_rejects_duplicate_json_keys(monkeypatch, capsys):
    _clear_env(monkeypatch)
    monkeypatch.setenv("PLUGLAYER_ENV_JSON", '{"TOKEN":"one","TOKEN":"two"}')

    assert pl.cmd_env_payload([]) == 1
    assert "duplicate" in capsys.readouterr().err


def test_env_payload_rejects_symlink_file(monkeypatch, capsys, tmp_path):
    _clear_env(monkeypatch)
    target = tmp_path / "target.env"
    target.write_text("TOKEN=secret", encoding="utf-8")
    link = tmp_path / "linked.env"
    link.symlink_to(target)
    monkeypatch.setenv("PLUGLAYER_ENV_FILE", str(link))

    assert pl.cmd_env_payload([]) == 1
    assert "symlink" in capsys.readouterr().err


def test_env_payload_rejects_implicit_clear_all(monkeypatch, capsys):
    _clear_env(monkeypatch)
    monkeypatch.setenv("PLUGLAYER_MERGE", "false")

    assert pl.cmd_env_payload([]) == 1
    assert "explicit" in capsys.readouterr().err


def test_action_calls_secure_import_endpoint():
    action = (HELPER.parents[1] / "apply-env-and-restart" / "action.yml").read_text(encoding="utf-8")
    assert "/env/import" in action
    assert "env_file:" in action
    assert "env_text:" in action
    assert "umask 077" in action
