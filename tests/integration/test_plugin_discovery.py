"""Real Hermes context-engine discovery for this repo's plugin directory.

Everything else in the suite fakes the host. This module does not: it runs the
*real* ``plugins.context_engine.find_engine_dir`` / ``load_context_engine`` code
from a Hermes checkout against a throwaway ``$HERMES_HOME`` whose
``plugins/hermes-clm`` is either a symlink to this repo or a copy of it — the
same layout an operator installs — and asserts what the host actually gets:

* the directory passes ``_is_context_engine_dir`` (root ``__init__.py`` marker),
* ``register(ctx)`` survives Hermes' ``_EngineCollector`` (which offers
  ``register_context_engine`` and ``register_command`` but no ``register_tool``),
* the engine exposes the twelve ``clm_*`` schemas through ``get_tool_schemas()``
  and dispatches through ``handle_tool_call()``,
* ``/clm`` lands in the plugin command registry exactly once,
* a second load does not duplicate or raise.

Skipped unless a checkout is provided; nothing here reads or writes the
operator's real ``$HERMES_HOME`` or config.

Environment:
    CLM_HERMES_CHECKOUT  path to a Hermes checkout (``agent/``, ``plugins/``)
    CLM_HERMES_PYTHON    interpreter that can import it (default: this one)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

HERMES_CHECKOUT = os.environ.get("CLM_HERMES_CHECKOUT", "").strip()
HERMES_PYTHON = os.environ.get("CLM_HERMES_PYTHON", "").strip() or sys.executable

pytestmark = pytest.mark.skipif(
    not HERMES_CHECKOUT or not (Path(HERMES_CHECKOUT) / "plugins" / "context_engine").is_dir(),
    reason="set CLM_HERMES_CHECKOUT to a Hermes checkout to run real discovery",
)

PROBE = r'''
"""Load hermes-clm the way Hermes does; print one JSON line."""
import json
import os
import sys

sys.path.insert(0, os.environ["CLM_HERMES_CHECKOUT"])

import hermes_cli.plugins as hermes_plugins


class RecordingManager:
    """Stands in for the plugin manager: captures engine slash commands."""

    _plugin_commands = {}


hermes_plugins.get_plugin_manager = lambda: RecordingManager

from plugins.context_engine import find_engine_dir, load_context_engine

name = "hermes-clm"
engine_dir = find_engine_dir(name)
result = {
    "hermes_home": os.environ.get("HERMES_HOME"),
    "dir": str(engine_dir) if engine_dir else None,
    "dir_name": engine_dir.name if engine_dir else None,
}

engine = load_context_engine(name) if engine_dir else None
result["engine_class"] = type(engine).__name__ if engine else None
result["engine_name"] = getattr(engine, "name", None)
result["handles_tool_calls"] = callable(getattr(engine, "handle_tool_call", None))
result["tool_schemas"] = []
if engine is not None:
    schemas = engine.get_tool_schemas()
    result["tool_schemas"] = [schema.get("name") for schema in schemas]
    result["schema_duplicates"] = len(schemas) - len(set(result["tool_schemas"]))
    result["unknown_tool"] = json.loads(engine.handle_tool_call("clm_not_a_tool", {}))
    status = json.loads(engine.handle_tool_call("clm_status", {}))
    result["status"] = status
    result["engine_config"] = status.get("config")

# A second load must produce a usable engine and must not raise.
second = load_context_engine(name) if engine_dir else None
result["second_load_ok"] = second is not None
result["commands"] = sorted(RecordingManager._plugin_commands)
result["command_entries"] = {
    key: value.get("plugin") for key, value in RecordingManager._plugin_commands.items()
}
result["tool_count_after_second_load"] = (
    len(second.get_tool_schemas()) if second is not None else None
)
print("CLM_DISCOVERY_JSON=" + json.dumps(result))
'''


def _installer(kind: str):
    def install(plugin_dir: Path) -> None:
        plugin_dir.parent.mkdir(parents=True, exist_ok=True)
        if kind == "symlink":
            plugin_dir.symlink_to(REPO_ROOT, target_is_directory=True)
            return
        shutil.copytree(
            REPO_ROOT,
            plugin_dir,
            symlinks=True,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                "venv",
                "__pycache__",
                "*.pyc",
                ".pytest_cache",
                ".ruff_cache",
                ".mypy_cache",
                "benchmarks",
            ),
        )

    return install


def run_discovery(home: Path) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith("HERMES_")}
    env.update(
        {
            "HERMES_HOME": str(home),
            "CLM_HERMES_CHECKOUT": HERMES_CHECKOUT,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": "",
        }
    )
    proc = subprocess.run(
        [HERMES_PYTHON, "-c", PROBE],
        cwd=str(home),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        pytest.skip(
            "Hermes checkout is not importable by this interpreter "
            f"({HERMES_PYTHON}); set CLM_HERMES_PYTHON.\n{proc.stderr[-2000:]}"
        )
    for line in proc.stdout.splitlines():
        if line.startswith("CLM_DISCOVERY_JSON="):
            return json.loads(line.split("=", 1)[1])
    raise AssertionError(f"probe produced no result:\n{proc.stdout}\n{proc.stderr}")


@pytest.fixture(params=["symlink", "copy", "copy+config"])
def discovered(request, tmp_path):
    kind, _, configured = request.param.partition("+")
    home = tmp_path / f"hermes-home-{kind}{'-config' if configured else ''}"
    home.mkdir(mode=0o700)
    _installer(kind)(home / "plugins" / "hermes-clm")
    if configured:
        (home / "config.yaml").write_text(
            "clm:\n  aux_enabled: false\n  reserve_tokens: 4096\n", encoding="utf-8"
        )
    production = Path(os.path.expanduser("~")) / ".hermes" / "config.yaml"
    before = production.stat().st_mtime_ns if production.exists() else None
    result = run_discovery(home)
    after = production.stat().st_mtime_ns if production.exists() else None
    result["_home"] = str(home)
    result["_kind"] = request.param
    result["_configured"] = bool(configured)
    result["_production_config"] = (str(production), before, after)
    return result


def test_host_finds_the_engine_directory(discovered):
    assert discovered["dir_name"] == "hermes-clm", discovered["dir"]
    assert discovered["dir"], "find_engine_dir() returned nothing"
    assert discovered["hermes_home"] == discovered["_home"], "isolated HERMES_HOME was not used"


def test_host_loads_a_clm_context_engine(discovered):
    assert discovered["engine_class"] == "CLMEngine"
    assert discovered["engine_name"] == "hermes-clm"
    assert discovered["second_load_ok"] is True


def test_engine_advertises_the_clm_tool_schemas(discovered):
    tools = discovered["tool_schemas"]
    assert len(tools) == 12, tools
    assert discovered["schema_duplicates"] == 0
    assert "clm_status" in tools and "clm_accept_proposal" in tools
    assert discovered["tool_count_after_second_load"] == 12


def test_engine_dispatch_is_json_and_rejects_unknown_names(discovered):
    assert discovered["handles_tool_calls"] is True
    assert discovered["unknown_tool"]["ok"] is False
    assert discovered["unknown_tool"]["errors"][0]["code"] == "unknown_tool"
    assert discovered["status"]["engine"] == "hermes-clm"


def test_operator_command_registers_exactly_once(discovered):
    assert discovered["commands"] == ["clm"], discovered["commands"]
    assert discovered["command_entries"] == {"clm": "context-engine:hermes-clm"}


def test_discovery_left_no_production_config_behind(discovered):
    home = Path(discovered["_home"])
    assert (home / "plugins" / "hermes-clm").exists()
    if not discovered["_configured"]:
        assert not (home / "config.yaml").exists(), "discovery must not create a profile config"
    path, before, after = discovered["_production_config"]
    assert before == after, f"discovery touched the operator config at {path}"


def test_clm_config_reaches_the_engine_through_discovery(discovered):
    config = discovered["engine_config"] or {}
    if discovered["_configured"]:
        assert config.get("reserve_tokens") == 4096, config
        assert config.get("aux_enabled") is False, config
    else:
        assert config.get("reserve_tokens") == 2048, config
