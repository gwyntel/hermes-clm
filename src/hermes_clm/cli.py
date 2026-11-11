"""Command-line entry point for hermes-clm.

Only the standard library is used, and importing this module never imports Hermes.
The offline benchmark harness is a development tool that lives outside the wheel; the
``bench`` command locates a source checkout and fails with a clear message otherwise.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import sys
from collections.abc import Sequence
from pathlib import Path

from ._version import __version__

_BENCHMARK_DIRNAME = "benchmarks"
_REPO_MARKERS = ("pyproject.toml", "README.md")


def _find_benchmarks_dir(explicit: str | None) -> Path | None:
    """Return the benchmark package directory, or ``None`` outside a source checkout."""
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        return candidate if (candidate / "runner.py").is_file() else None

    candidates: list[Path] = []
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        current = start
        for _ in range(6):
            if all((current / marker).is_file() for marker in _REPO_MARKERS):
                candidates.append(current / _BENCHMARK_DIRNAME)
            if current.parent == current:
                break
            current = current.parent

    for candidate in candidates:
        if (candidate / "runner.py").is_file():
            return candidate
    return None


def _cmd_version(_: argparse.Namespace) -> int:
    print(f"hermes-clm {__version__}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    hermes_spec = importlib.util.find_spec("hermes_cli")
    hermes_home = os.environ.get("HERMES_HOME")
    benchmarks = _find_benchmarks_dir(args.benchmarks_dir)
    report = {
        "package": "hermes-clm",
        "version": __version__,
        "python": platform.python_version(),
        "python_ok": sys.version_info >= (3, 11),
        "hermes_cli_importable": hermes_spec is not None,
        "hermes_home_set": bool(hermes_home),
        "benchmarks_available": benchmarks is not None,
        "benchmarks_dir": str(benchmarks) if benchmarks else None,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["python_ok"] else 1


def _cmd_bench(args: argparse.Namespace) -> int:
    benchmarks = _find_benchmarks_dir(args.benchmarks_dir)
    if benchmarks is None:
        print(
            "error: offline benchmarks are not installed with the wheel; run "
            "`hermes-clm bench` from a source checkout or pass --benchmarks-dir",
            file=sys.stderr,
        )
        return 2

    sys.path.insert(0, str(benchmarks.parent))
    try:
        from benchmarks.runner import (
            BenchmarkError,
            run_all,
            write_reports,
        )
    except ImportError as exc:  # pragma: no cover - broken checkout only
        print(f"error: cannot import benchmark harness: {exc}", file=sys.stderr)
        return 2

    try:
        names = args.strategies.split(",") if args.strategies else None
        report = run_all(fixtures_dir=args.fixtures, strategy_names=names)
    except BenchmarkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json or args.markdown:
        write_reports(report, json_path=args.json, markdown_path=args.markdown)
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if not (args.json or args.markdown or args.out):
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-clm",
        description="Model-directed context management for Hermes Agent.",
    )
    parser.add_argument("--version", action="version", version=f"hermes-clm {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    version = sub.add_parser("version", help="print the package version")
    version.set_defaults(func=_cmd_version)

    doctor = sub.add_parser("doctor", help="report runtime and checkout capabilities as JSON")
    doctor.add_argument(
        "--benchmarks-dir",
        default=None,
        help="explicit path to the source checkout's benchmarks directory",
    )
    doctor.set_defaults(func=_cmd_doctor)

    bench = sub.add_parser("bench", help="run the deterministic offline A/B/C/D benchmarks")
    bench.add_argument("--benchmarks-dir", default=None, help="explicit benchmarks directory")
    bench.add_argument("--fixtures", default=None, help="directory of benchmark fixtures")
    bench.add_argument(
        "--strategies",
        default=None,
        help="comma-separated strategy names (default: A,B,C,D)",
    )
    bench.add_argument("--json", default=None, help="write the JSON report to this path")
    bench.add_argument("--markdown", default=None, help="write the Markdown report to this path")
    bench.add_argument("--out", default=None, help="write the JSON report to this file")
    bench.set_defaults(func=_cmd_bench)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
