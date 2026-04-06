from pathlib import Path
from types import SimpleNamespace

import pytest

from core.strategy_loader import (
    LoadedStrategy,
    StrategyLoadError,
    StrategyLoader,
    resolve_strategy_params,
)


def _write_strategy(tmp_path: Path, name: str, body: str) -> Path:
    file_path = tmp_path / name
    file_path.write_text(body, encoding="utf-8")
    return file_path


def test_load_strategy_extracts_metadata_and_schema(tmp_path):
    file_path = _write_strategy(
        tmp_path,
        "demo_strategy.py",
        """
def get_info():
    return {"name": "Demo", "version": "1.0"}

def get_params():
    return {"qty": {"type": "int", "default": 1, "min": 1}}

def on_start(params, connector):
    return None

def on_stop(params, connector):
    return None

def on_tick(tick_data, params, connector):
    return None
""",
    )
    loader = StrategyLoader()

    loaded = loader.load("demo", str(file_path))

    assert loaded.info["name"] == "Demo"
    assert loaded.params_schema["qty"]["default"] == 1
    assert loader.get("demo") is loaded


def test_load_strategy_rejects_missing_required_function(tmp_path):
    file_path = _write_strategy(
        tmp_path,
        "bad_strategy.py",
        """
def get_info():
    return {"name": "Bad"}

def get_params():
    return {}

def on_start(params, connector):
    return None

def on_stop(params, connector):
    return None
""",
    )
    loader = StrategyLoader()

    with pytest.raises(StrategyLoadError):
        loader.load("bad", str(file_path))


def test_resolve_strategy_params_applies_defaults_and_coercion():
    schema = {
        "qty": {"type": "int", "default": 1, "min": 1},
        "risk_pct": {"type": "float", "default": 0.5, "min": 0.1},
        "enabled": {"type": "bool", "default": False},
        "mode": {"type": "select", "default": "market", "options": ["market", "limit"]},
    }

    resolved, error = resolve_strategy_params({"qty": 2.0, "enabled": "true"}, schema)

    assert error is None
    assert resolved["qty"] == 2
    assert resolved["risk_pct"] == 0.5
    assert resolved["enabled"] is True
    assert resolved["mode"] == "market"


def test_resolve_strategy_params_rejects_invalid_select():
    schema = {
        "mode": {"type": "select", "default": "market", "options": ["market", "limit"]}
    }

    resolved, error = resolve_strategy_params({"mode": "iceberg"}, schema)

    assert resolved == {}
    assert "не в допустимых значениях" in error


def test_call_on_start_rejects_invalid_params_before_strategy_code_runs():
    calls = []
    module = SimpleNamespace(
        get_info=lambda: {"name": "Demo"},
        get_params=lambda: {"qty": {"type": "int", "default": 1, "min": 1}},
        on_start=lambda params, connector: calls.append(params),
        on_stop=lambda params, connector: None,
        on_tick=lambda tick_data, params, connector: None,
    )
    loaded = LoadedStrategy("sid", module, "demo.py")

    ok = loaded.call_on_start({"qty": 0}, connector=None)

    assert ok is False
    assert calls == []