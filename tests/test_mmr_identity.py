from __future__ import annotations

from pathlib import Path

from rl_tracker.mmr_identity import detect_player, is_launch_log_current


def write_log(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "Launch.log"
    p.write_text(content, encoding="utf-8")
    return p


def test_detect_steam_id(tmp_path: Path) -> None:
    log = write_log(
        tmp_path,
        "noise line\n"
        "[0000.00] OnlineSubsystemSteam: Logged in as 76561198000000001\n"
        "more noise\n",
    )
    ident = detect_player(log)
    assert ident is not None
    assert ident.platform == "steam"
    assert ident.id_or_name == "76561198000000001"


def test_detect_epic_display_name(tmp_path: Path) -> None:
    log = write_log(
        tmp_path,
        'OnlineSubsystemEpic: PlayerInfo DisplayName="CoolPlayer123"\n',
    )
    ident = detect_player(log)
    assert ident is not None
    assert ident.platform == "epic"
    assert ident.id_or_name == "CoolPlayer123"


def test_most_recent_wins(tmp_path: Path) -> None:
    log = write_log(
        tmp_path,
        "OnlineSubsystemSteam: Logged in as 76561198000000001\n"
        "later line\n"
        "OnlineSubsystemSteam: Logged in as 76561198999999999\n",
    )
    ident = detect_player(log)
    assert ident is not None
    assert ident.id_or_name == "76561198999999999"


def test_returns_none_on_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.log"
    assert detect_player(missing) is None


def test_returns_none_on_empty(tmp_path: Path) -> None:
    log = write_log(tmp_path, "nothing useful here\n")
    assert detect_player(log) is None


def test_is_launch_log_current(tmp_path: Path) -> None:
    log = write_log(tmp_path, "x")
    assert is_launch_log_current(log)
    assert not is_launch_log_current(tmp_path / "missing.log")
