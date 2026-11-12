"""Real-host prompt-section integration: general plugin + engine in one profile.

The unit tests fake the context. This module runs the *actual* Hermes plugin
loader from ``CLM_HERMES_CHECKOUT`` against a throwaway ``$HERMES_HOME`` whose
``plugins/hermes-clm`` is a symlink to this repo, and asserts what the host does
with the registered section:

* a profile that enables the plugin **and** selects ``context.engine:
  hermes-clm`` loads engine, ``/clm`` and the twelve tools, and renders the
  guidance section exactly once (one container, one heading);
* the frozen copy is recoverable from the persisted prompt without re-running
  plugin code (a resumed session);
* a profile that selects another engine renders nothing — the guidance is
  withheld while the engine registration is unchanged;
* the engine-only load path (no ``plugins.enabled`` entry, an
  ``_EngineCollector`` with no section API) still loads a working engine.

The probe also loads the engine the way ``agent_init`` does, so the selected
profile exercises the directory engine loader *and* the general plugin in one
interpreter — not merely the plugin manager's engine slot.

Skipped only when no checkout is configured, or when a prerequisite import of
that checkout fails in this interpreter. Any probe failure after that point is a
test failure: a broken plugin must never turn into a green skip.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

HERMES_CHECKOUT = os.environ.get("CLM_HERMES_CHECKOUT", "").strip()
HERMES_PYTHON = os.environ.get("CLM_HERMES_PYTHON", "").strip() or sys.executable

pytestmark = pytest.mark.skipif(
    not HERMES_CHECKOUT or not (Path(HERMES_CHECKOUT) / "plugins" / "context_engine").is_dir(),
    reason="set CLM_HERMES_CHECKOUT to a Hermes checkout to run the real host probe",
)

PROBE = r'''
"""Load this repo the way Hermes does; print one JSON line."""
import json
import os
import sys

sys.path.insert(0, os.environ["CLM_HERMES_CHECKOUT"])

mode = os.environ["CLM_PROBE_MODE"]
result = {"mode": mode, "hermes_home": os.environ.get("HERMES_HOME")}


def _sections_of(rendered):
    return [
        {
            "id": item.id,
            "plugin": item.plugin,
            "position": item.position,
            "chars": len(item.content),
            "content": item.content,
        }
        for item in rendered
    ]


def _choose_engine(name):
    """Mirror ``agent_init._select_context_engine`` lookup order.

    Importing ``agent.agent_init`` pulls the whole host config stack, which is
    not needed to prove which engine ``context.engine`` resolves to: the
    directory loader runs first and the plugin manager's slot is the fallback.
    """

    from plugins.context_engine import load_context_engine

    engine = load_context_engine(name)
    if engine is not None:
        return engine, "directory"
    from hermes_cli.plugins import get_plugin_context_engine

    candidate = get_plugin_context_engine()
    if candidate is not None and getattr(candidate, "name", None) == name:
        return candidate, "plugin-manager"
    return None, None


if mode == "engine_only":
    from plugins.context_engine import find_engine_dir, load_context_engine

    engine_dir = find_engine_dir("hermes-clm")
    engine = load_context_engine("hermes-clm") if engine_dir else None
    result["dir_name"] = engine_dir.name if engine_dir else None
    result["engine_name"] = getattr(engine, "name", None)
    result["tool_schemas"] = [s.get("name") for s in engine.get_tool_schemas()] if engine else []
    result["status_ok"] = bool(engine and engine.get_status().get("engine") == "hermes-clm")
    # Same selection the agent makes: context.engine -> directory loader first.
    chosen, source = _choose_engine("hermes-clm")
    result["selected_engine_name"] = getattr(chosen, "name", None)
    result["selection_source"] = source
    result["selected_engine_tool_count"] = len(chosen.get_tool_schemas()) if chosen else 0
    # The collector has no section API: reading the profile's section registry
    # must stay empty instead of the plugin crashing on the missing method.
    from hermes_cli.plugins import get_plugin_manager

    result["section_registry"] = sorted(get_plugin_manager()._system_prompt_sections)
    result["exception"] = None
    print("CLM_PROMPT_JSON=" + json.dumps(result))
    raise SystemExit(0)

import hermes_cli.plugins as hermes_plugins
from hermes_cli.plugins import (
    format_system_prompt_sections,
    get_plugin_manager,
    render_system_prompt_sections,
)

hermes_plugins.discover_plugins(force=True)
manager = get_plugin_manager()

# Mirror the session info the host actually passes (``agent/system_prompt.py``):
# the profile name is what a section callable resolves its own selection from.
_profile_name = os.environ.get("CLM_PROBE_PROFILE", "custom")
rendered = render_system_prompt_sections(
    {"session_id": "prompt-section-probe", "profile_name": _profile_name}
)
again = render_system_prompt_sections(
    {"session_id": "another-session", "platform": "cli", "profile_name": _profile_name}
)
prompt = format_system_prompt_sections(rendered) + "\n\nConversation started: 2026-01-01 00:00\n"

result["sections"] = _sections_of(rendered)
result["section_registry"] = sorted(manager._system_prompt_sections)
result["stable_across_sessions"] = _sections_of(rendered) == _sections_of(again)
result["heading_occurrences"] = prompt.count("## Plugin Context: hermes-clm.usage")
result["container_occurrences"] = prompt.count("<!-- hermes-plugin-sections:start -->")
result["engine_name"] = getattr(manager._context_engine, "name", None)
result["commands"] = sorted(manager._plugin_commands)
engine = manager._context_engine
schemas = engine.get_tool_schemas() if engine is not None else []
names = [schema.get("name") for schema in schemas]
result["tool_schemas"] = names
result["schema_duplicates"] = len(names) - len(set(names))

# Simultaneous directory-engine load: the agent does not consume the plugin
# manager's engine slot when context.engine names an engine on disk. Load that
# path in the same interpreter as the general plugin and check both instances.
from plugins.context_engine import load_context_engine

directory_engine = load_context_engine("hermes-clm")
directory_names = (
    [schema.get("name") for schema in directory_engine.get_tool_schemas()]
    if directory_engine is not None
    else []
)
result["directory_engine_name"] = getattr(directory_engine, "name", None)
result["directory_tool_schemas"] = directory_names
result["directory_is_manager_instance"] = (
    directory_engine is not None and directory_engine is manager._context_engine
)

chosen, source = _choose_engine("hermes-clm")
result["selected_engine_name"] = getattr(chosen, "name", None)
result["selection_source"] = source
result["selected_engine_tool_count"] = (
    len(chosen.get_tool_schemas()) if chosen is not None else 0
)

# Dual enablement: the plugin lane must not advertise a second copy of the
# twelve names, and it must not hold the store writer. The selected engine
# opens the session store (exclusive flock) and serves a tool call.
result["plugin_lane_tool_names"] = sorted(
    name for name in manager._plugin_tool_names if name.startswith("clm_")
)
result["manager_engine_store"] = (
    manager._context_engine.get_status().get("store")
    if manager._context_engine is not None
    else None
)
result["selected_tool_status_ok"] = None
result["selected_tool_status_error"] = None
if chosen is not None:
    chosen.service_for_session("clm-dual-lane-probe")
    payload = json.loads(
        chosen.handle_tool_call("clm_status", {}, session_id="clm-dual-lane-probe")
    )
    result["selected_tool_status_ok"] = payload.get("ok")
    result["selected_tool_status_error"] = payload.get("errors")
    result["selected_engine_store_after"] = chosen.get_status().get("store")
else:
    result["selected_engine_store_after"] = None

# Resumed session: the host must recover the frozen bytes from the persisted
# prompt and must not re-run plugin code to do it.
from agent import system_prompt

def _boom(*_args, **_kwargs):
    raise AssertionError("frozen resume re-rendered plugin sections")

hermes_plugins.render_system_prompt_sections = _boom
holder = type("Agent", (), {})()
holder._cached_system_prompt = prompt
frozen = system_prompt._frozen_plugin_prompt_sections(holder)
result["frozen_sections"] = _sections_of(frozen)

from hermes_clm.guidance import SYSTEM_PROMPT_SECTION_TEXT

result["content_matches_guidance"] = bool(
    rendered and rendered[0].content == SYSTEM_PROMPT_SECTION_TEXT
)
result["exception"] = None
print("CLM_PROMPT_JSON=" + json.dumps(result))
'''


def _install_repo(plugin_dir: Path) -> None:
    plugin_dir.parent.mkdir(parents=True, exist_ok=True)
    plugin_dir.symlink_to(REPO_ROOT, target_is_directory=True)


def _profile(home: Path, config: str) -> None:
    home.mkdir(mode=0o700)
    (home / "config.yaml").write_text(config, encoding="utf-8")


def _probe_env(**extra: str) -> dict[str, str]:
    """A clean environment: no inherited ``HERMES_*`` state, no ambient PYTHONPATH."""

    env = {key: value for key, value in os.environ.items() if not key.startswith("HERMES_")}
    env.update(
        {
            "CLM_HERMES_CHECKOUT": HERMES_CHECKOUT,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": "",
        }
    )
    env.update(extra)
    return env


#: Modules the probe imports. Kept explicit so a skip names the missing piece.
PREREQUISITES = (
    "hermes_cli.plugins",
    "agent.system_prompt",
    "plugins.context_engine",
)


def _prerequisite_error() -> str | None:
    """Import the Hermes surface once; return a skip reason on failure.

    This is the *only* condition under which a non-zero Hermes subprocess may
    skip: the configured interpreter cannot import the checkout at all. Anything
    after this point (plugin import, registration, rendering) is a real failure.
    """

    code = "import sys; sys.path.insert(0, {!r}); import {}".format(
        HERMES_CHECKOUT, ", ".join(PREREQUISITES)
    )
    proc = subprocess.run(
        [HERMES_PYTHON, "-c", code],
        cwd=str(REPO_ROOT),
        env=_probe_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode == 0:
        return None
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return detail[-1] if detail else f"exit {proc.returncode}"


@pytest.fixture(scope="module", autouse=True)
def _hermes_prerequisites() -> None:
    error = _prerequisite_error()
    if error is not None:
        pytest.skip(
            f"Hermes checkout at {HERMES_CHECKOUT} is not importable by "
            f"{HERMES_PYTHON} ({error}); set CLM_HERMES_PYTHON"
        )


def _run(home: Path, mode: str) -> dict:
    proc = subprocess.run(
        [HERMES_PYTHON, "-c", PROBE],
        cwd=str(home),
        env=_probe_env(HERMES_HOME=str(home), CLM_PROBE_MODE=mode),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        # The prerequisite check already proved the interpreter can import
        # Hermes, so this is the plugin or the probe failing: never a skip.
        raise AssertionError(
            f"Hermes probe failed (mode={mode!r}, exit={proc.returncode}); "
            "this is a plugin/probe failure, not a missing prerequisite.\n"
            f"--- stdout ---\n{proc.stdout[-4000:]}\n"
            f"--- stderr ---\n{proc.stderr[-4000:]}"
        )
    for line in proc.stdout.splitlines():
        if line.startswith("CLM_PROMPT_JSON="):
            return json.loads(line.split("=", 1)[1])
    raise AssertionError(f"probe produced no result:\n{proc.stdout}\n{proc.stderr}")


@pytest.fixture(scope="module")
def selected(tmp_path_factory):
    """Plugin enabled *and* ``context.engine: hermes-clm``: both lanes active."""

    home = tmp_path_factory.mktemp("clm-selected") / "home"
    _profile(
        home,
        "plugins:\n  enabled:\n    - hermes-clm\ncontext:\n  engine: hermes-clm\n",
    )
    _install_repo(home / "plugins" / "hermes-clm")
    return _run(home, "selected")


@pytest.fixture(scope="module")
def inactive(tmp_path_factory):
    """Plugin enabled for another engine: guidance must be withheld."""

    home = tmp_path_factory.mktemp("clm-inactive") / "home"
    _profile(
        home,
        "plugins:\n  enabled:\n    - hermes-clm\ncontext:\n  engine: compressor\n",
    )
    _install_repo(home / "plugins" / "hermes-clm")
    return _run(home, "inactive")


@pytest.fixture(scope="module")
def engine_only(tmp_path_factory):
    """Directory-loaded engine, no general-plugin entry: no section API."""

    home = tmp_path_factory.mktemp("clm-engine-only") / "home"
    _profile(home, "context:\n  engine: hermes-clm\n")
    _install_repo(home / "plugins" / "hermes-clm")
    return _run(home, "engine_only")


# --------------------------------------------------------------------------- #
# Both lanes, selected
# --------------------------------------------------------------------------- #


def test_selected_profile_loads_both_lanes(selected):
    assert selected["engine_name"] == "hermes-clm"
    assert selected["commands"] == ["clm"]
    assert len(selected["tool_schemas"]) == 12
    assert selected["schema_duplicates"] == 0, "the two lanes must not duplicate tool names"
    assert selected["section_registry"] == ["hermes-clm.usage"]


def test_selected_profile_also_loads_the_directory_engine_in_process(selected):
    """The agent's actual path is the directory loader, alongside the plugin."""

    assert selected["directory_engine_name"] == "hermes-clm"
    assert len(selected["directory_tool_schemas"]) == 12
    assert len(set(selected["directory_tool_schemas"])) == 12
    assert sorted(selected["directory_tool_schemas"]) == sorted(selected["tool_schemas"]), (
        "both load paths must advertise the same twelve tools"
    )
    assert selected["directory_is_manager_instance"] is False, (
        "the directory loader must build its own engine, not hand back the plugin's slot"
    )
    assert selected["selected_engine_name"] == "hermes-clm"
    assert selected["selection_source"] == "directory", (
        "context.engine must resolve through the directory loader, not the plugin-manager fallback"
    )
    assert selected["selected_engine_tool_count"] == 12


def test_selected_profile_registers_no_duplicate_clm_tool_names(selected):
    """One tool surface: the selected engine's. The plugin lane adds none."""

    assert selected["plugin_lane_tool_names"] == [], (
        "the general plugin lane registered clm_* names the engine already injects"
    )
    # The plugin-manager engine instance is a fallback provider only; it must
    # not open a store for the selected engine's session (that would be a second
    # writer, and StoreBusyError would surface as no_session in the tools).
    assert selected["manager_engine_store"] == "detached"


def test_selected_engine_serves_tool_calls_without_a_store_conflict(selected):
    assert selected["selected_engine_store_after"] == "attached"
    assert selected["selected_tool_status_ok"] is True, selected["selected_tool_status_error"]


def test_selected_profile_renders_the_section_exactly_once(selected):
    sections = selected["sections"]
    assert len(sections) == 1, sections
    section = sections[0]
    assert section["id"] == "hermes-clm.usage"
    assert section["position"] == "after_memory"
    assert section["plugin"] == "hermes-clm"
    assert 0 < section["chars"] <= 4_000
    assert selected["heading_occurrences"] == 1
    assert selected["container_occurrences"] == 1


def test_section_text_is_the_static_guidance(selected):
    assert selected["content_matches_guidance"] is True
    assert selected["stable_across_sessions"] is True, "static text must not vary per session"


def test_resumed_session_recovers_the_frozen_section(selected):
    frozen = selected["frozen_sections"]
    assert len(frozen) == 1
    assert frozen[0]["id"] == "hermes-clm.usage"
    assert frozen[0]["content"] == selected["sections"][0]["content"]
    assert frozen[0]["plugin"] == "persisted-prompt", "restored from prompt bytes, not re-rendered"


# --------------------------------------------------------------------------- #
# Plugin enabled, engine not selected
# --------------------------------------------------------------------------- #


def test_unselected_engine_renders_no_guidance(inactive):
    assert inactive["engine_name"] == "hermes-clm", "the engine registration itself is unchanged"
    assert inactive["commands"] == ["clm"]
    assert inactive["section_registry"] == ["hermes-clm.usage"], (
        "registered once, so a routed turn whose own profile selects the engine can still "
        "qualify; the render below is what must stay empty"
    )
    assert inactive["sections"] == []
    assert inactive["heading_occurrences"] == 0
    assert inactive["container_occurrences"] == 0
    assert inactive["frozen_sections"] == []
    assert inactive["content_matches_guidance"] is False


# --------------------------------------------------------------------------- #
# Engine-only load path
# --------------------------------------------------------------------------- #


def test_engine_only_path_still_loads_without_the_section_api(engine_only):
    assert engine_only["dir_name"] == "hermes-clm"
    assert engine_only["engine_name"] == "hermes-clm"
    assert engine_only["status_ok"] is True
    assert len(engine_only["tool_schemas"]) == 12
    assert engine_only["selected_engine_name"] == "hermes-clm"
    assert engine_only["selection_source"] == "directory"
    assert engine_only["selected_engine_tool_count"] == 12
    assert engine_only["section_registry"] == [], (
        "no section API on this path, so nothing is claimed"
    )
    assert engine_only["exception"] is None
