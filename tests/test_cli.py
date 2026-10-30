"""CLI parser and lightweight command tests."""

from __future__ import annotations

import pytest

from envmon.cli import build_parser, main


@pytest.mark.parametrize(
    "argv",
    [
        ["--mode", "simulator", "--log-level", "debug", "sample-once"],
        ["sample-once", "--mode", "simulator", "--log-level", "debug"],
        ["sample-once", "--mode=simulator", "--log-level=debug"],
    ],
)
def test_common_options_work_on_either_side(argv) -> None:
    args = build_parser().parse_args(argv)
    assert args.command == "sample-once"
    assert args.mode == "simulator"
    assert args.log_level == "debug"


def test_common_options_are_optional() -> None:
    args = build_parser().parse_args(["run"])
    assert args.mode is None
    assert args.config is None
    assert args.log_level is None
    assert args.host is None
    assert args.port is None


def test_subcommand_specific_defaults() -> None:
    aggregate = build_parser().parse_args(["aggregate", "--days", "3"])
    assert aggregate.days == 3
    assert aggregate.day is None

    export = build_parser().parse_args(["export", "-o", "out.csv"])
    assert export.hours == 24
    assert export.output == "out.csv"


def test_missing_config_file_fails(monkeypatch, tmp_path, capsys) -> None:
    missing = tmp_path / "missing.yaml"
    code = main(["--config", str(missing), "version"])
    assert code == 2
    assert "config file not found" in capsys.readouterr().err


def test_version_command(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("ENVMON_DATABASE__URL", f"sqlite:///{tmp_path / 'cli.db'}")
    # configure_logging(force=True) would replace pytest's capture handler.
    monkeypatch.setattr("envmon.cli.configure_logging", lambda *args, **kwargs: None)
    code = main(["version"])
    output = capsys.readouterr().out
    assert code == 0
    assert "envmon" in output
    assert "database:" in output
