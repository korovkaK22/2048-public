"""OS-aware host path/binary resolution -- a SINGLE code path per tool.

No ``*_win`` / ``*_mac`` module fork: one resolver branches internally on the
runtime environment (env vars, filesystem layout, PATH, ``os.name``). These
tests exercise BOTH the Windows and the macOS branch on the same (Windows) CI
host:

* scrcpy-server resolution is path/PATH-based, so the Windows-zip layout, the
  macOS-zip layout and the Homebrew ``share/scrcpy`` layout all run directly;
* the two genuine OS switches -- the ``adb[.exe]`` suffix (import-time,
  ``sys.platform``) and the engine ``engine2048[.exe]`` name (call-time,
  ``os.name``) -- are covered by faking the platform ('Windows' vs 'Darwin').
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

import bot2048.runner.cli.tools as cli_tools
from bot2048.ai import go_engine


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"jar")
    return path


# --- scrcpy-server jar: one resolver, every host layout --------------------


def test_scrcpy_server_env_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jar = _touch(tmp_path / "custom" / "scrcpy-server")
    monkeypatch.setenv("BOT2048_SCRCPY_SERVER", str(jar))
    # A bundled jar also exists, but the explicit override must win.
    tools = tmp_path / "tools" / "scrcpy"
    _touch(tools / "scrcpy-win64-v4.0" / "scrcpy-server")
    monkeypatch.setattr(cli_tools, "_SCRCPY_TOOLS_DIR", tools)
    assert cli_tools._resolve_scrcpy_server_jar() == jar


def test_scrcpy_server_official_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jar = _touch(tmp_path / "scrcpy-server")
    monkeypatch.delenv("BOT2048_SCRCPY_SERVER", raising=False)
    monkeypatch.setenv("SCRCPY_SERVER_PATH", str(jar))
    monkeypatch.setattr(cli_tools, "_SCRCPY_TOOLS_DIR", tmp_path / "absent")
    monkeypatch.setattr(cli_tools.shutil, "which", lambda _n: None)
    assert cli_tools._resolve_scrcpy_server_jar() == jar


def test_scrcpy_server_bundled_windows_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = tmp_path / "tools" / "scrcpy"
    jar = _touch(tools / "scrcpy-win64-v4.0" / "scrcpy-server")
    monkeypatch.delenv("BOT2048_SCRCPY_SERVER", raising=False)
    monkeypatch.delenv("SCRCPY_SERVER_PATH", raising=False)
    monkeypatch.setattr(cli_tools, "_SCRCPY_TOOLS_DIR", tools)
    assert cli_tools._resolve_scrcpy_server_jar() == jar


def test_scrcpy_server_bundled_macos_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A macOS scrcpy zip unpacks the jar under a different subdir of the SAME
    # tools/scrcpy dir -- the recursive search finds it with no code fork.
    tools = tmp_path / "tools" / "scrcpy"
    jar = _touch(tools / "scrcpy-macos-aarch64-v4.0" / "scrcpy-server")
    monkeypatch.delenv("BOT2048_SCRCPY_SERVER", raising=False)
    monkeypatch.delenv("SCRCPY_SERVER_PATH", raising=False)
    monkeypatch.setattr(cli_tools, "_SCRCPY_TOOLS_DIR", tools)
    assert cli_tools._resolve_scrcpy_server_jar() == jar


def test_scrcpy_server_from_path_brew_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Homebrew (macOS): <prefix>/bin/scrcpy + <prefix>/share/scrcpy/scrcpy-server.
    prefix = tmp_path / "opt" / "homebrew"
    bin_scrcpy = _touch(prefix / "bin" / "scrcpy")
    jar = _touch(prefix / "share" / "scrcpy" / "scrcpy-server")
    monkeypatch.delenv("BOT2048_SCRCPY_SERVER", raising=False)
    monkeypatch.delenv("SCRCPY_SERVER_PATH", raising=False)
    monkeypatch.setattr(cli_tools, "_SCRCPY_TOOLS_DIR", tmp_path / "absent")
    monkeypatch.setattr(
        cli_tools.shutil, "which", lambda n: str(bin_scrcpy) if n == "scrcpy" else None
    )
    assert cli_tools._resolve_scrcpy_server_jar() == jar


def test_scrcpy_server_none_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BOT2048_SCRCPY_SERVER", raising=False)
    monkeypatch.delenv("SCRCPY_SERVER_PATH", raising=False)
    monkeypatch.setattr(cli_tools, "_SCRCPY_TOOLS_DIR", tmp_path / "absent")
    monkeypatch.setattr(cli_tools.shutil, "which", lambda _n: None)
    assert cli_tools._resolve_scrcpy_server_jar() is None


def test_ensure_push_skips_when_jar_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No jar -> best-effort push logs & returns; never spawns adb, never raises."""
    monkeypatch.setattr(cli_tools, "_resolve_scrcpy_server_jar", lambda: None)

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("adb must not be spawned when no jar is found")

    monkeypatch.setattr(cli_tools.subprocess, "run", _boom)
    cli_tools._ensure_scrcpy_server_pushed("adb", None)  # returns cleanly


# --- ffmpeg: PATH-first on every OS ----------------------------------------


def test_ffmpeg_prefers_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli_tools.shutil, "which", lambda n: "/usr/bin/ffmpeg" if n == "ffmpeg" else None
    )
    assert cli_tools._resolve_ffmpeg_exe() == "ffmpeg"


def test_ffmpeg_falls_back_to_plain_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_tools.shutil, "which", lambda _n: None)
    monkeypatch.setattr(cli_tools, "_BUNDLED_FFMPEG", Path("does") / "not" / "exist")
    assert cli_tools._resolve_ffmpeg_exe() == "ffmpeg"


# --- adb: repo-local if present, else PATH ---------------------------------


def test_adb_prefers_bundled_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled = _touch(tmp_path / "adb")
    monkeypatch.setattr(cli_tools, "_BUNDLED_ADB", bundled)
    assert cli_tools._resolve_adb_exe() == str(bundled)


def test_adb_falls_back_to_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_tools, "_BUNDLED_ADB", Path("nope") / "adb")
    assert cli_tools._resolve_adb_exe() == "adb"


# --- the two real OS switches: fake Windows vs Darwin -----------------------


@pytest.mark.parametrize(
    ("fake_platform", "expected_name"),
    [("win32", "adb.exe"), ("darwin", "adb"), ("linux", "adb")],
)
def test_bundled_adb_suffix_per_platform(fake_platform: str, expected_name: str) -> None:
    """``.exe`` suffix is applied ONLY on Windows (import-time sys.platform)."""
    real = sys.platform
    try:
        sys.platform = fake_platform
        reloaded = importlib.reload(cli_tools)
        assert reloaded._BUNDLED_ADB.name == expected_name
    finally:
        sys.platform = real
        importlib.reload(cli_tools)  # restore the module to the real host


@pytest.mark.parametrize(
    ("fake_os_name", "expected_name"),
    [("nt", "engine2048.exe"), ("posix", "engine2048")],
)
def test_engine_default_binary_name_per_os(
    monkeypatch: pytest.MonkeyPatch, fake_os_name: str, expected_name: str
) -> None:
    """The live Go engine binary is ``engine2048[.exe]`` -- ``.exe`` only on nt.

    This is the binary that actually serves the live stdio protocol
    (ping/search/prefetch); ``tools/ntuple`` is train/probe/bench/arena only.
    """
    # Pin the tools dir (a real host Path) BEFORE faking os.name, so only the
    # name ternary reacts -- faking os.name globally would break Path() itself.
    fixed = go_engine._tools_dir()
    monkeypatch.setattr(go_engine, "_tools_dir", lambda: fixed)
    monkeypatch.setattr(go_engine.os, "name", fake_os_name)
    assert go_engine._default_binary_path().name == expected_name
