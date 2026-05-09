"""One-shot discovery tool: scan Rocket League's Launch.log for interesting
lines so we can decide what's worth parsing in rl_tracker/log_watcher.py.

Read-only. Prints a categorized sample of matching lines plus the top
"unknown but interesting-looking" lines (anything mentioning mmr/skill/rank/
playlist/match/region/forfeit/replay). No network, no writes.

Usage:
    python scripts/inspect_launch_log.py
    python scripts/inspect_launch_log.py --log "C:\\path\\to\\Launch.log"
    python scripts/inspect_launch_log.py --max-per-category 10 --tail 200000
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

# Each category is a name -> list of regex patterns. We tag each matching
# line with every category that hits, then print up to --max-per-category
# unique samples per category.
CATEGORIES: dict[str, list[re.Pattern[str]]] = {
    # The big one — anything MMR / skill rating shaped.
    "mmr_skill": [
        re.compile(r"\bMMR\b", re.IGNORECASE),
        re.compile(r"\bSkillRating\b", re.IGNORECASE),
        re.compile(r"\bMu\s*[:=]", re.IGNORECASE),  # TrueSkill mu
        re.compile(r"\bSigma\s*[:=]", re.IGNORECASE),
        re.compile(r"\bTier\b", re.IGNORECASE),
        re.compile(r"\bDivision\b", re.IGNORECASE),
        re.compile(r"\bRankPoints?\b", re.IGNORECASE),
        re.compile(r"\bSkill[_ ]?Update\b", re.IGNORECASE),
        re.compile(r"\bRatingChange\b", re.IGNORECASE),
        re.compile(r"\bELO\b", re.IGNORECASE),
    ],
    "playlist": [
        re.compile(r"Playlist[_ ]?ID", re.IGNORECASE),
        re.compile(r"PlaylistId", re.IGNORECASE),
        re.compile(r"\bPlaylist\b", re.IGNORECASE),
    ],
    "match_lifecycle": [
        re.compile(r"MatchGuid", re.IGNORECASE),
        re.compile(r"OnlineGame", re.IGNORECASE),
        re.compile(r"MatchEnded|MatchStart", re.IGNORECASE),
        re.compile(r"\bForfeit\b", re.IGNORECASE),
        re.compile(r"\bDisconnect\b", re.IGNORECASE),
    ],
    "map_arena": [
        re.compile(r"\bLoadMap\b", re.IGNORECASE),
        re.compile(r"\bMap\s*[:=]", re.IGNORECASE),
        re.compile(r"Stadium_", re.IGNORECASE),
    ],
    "party_queue": [
        re.compile(r"\bParty\b", re.IGNORECASE),
        re.compile(r"PartySize", re.IGNORECASE),
        re.compile(r"\bQueue", re.IGNORECASE),
    ],
    "region_ping": [
        re.compile(r"\bRegion\b", re.IGNORECASE),
        re.compile(r"\bPing\b", re.IGNORECASE),
        re.compile(r"Datacenter", re.IGNORECASE),
        re.compile(r"\bServerName\b", re.IGNORECASE),
    ],
    "bot_fill": [
        re.compile(r"\bBot\b", re.IGNORECASE),
        re.compile(r"AddBot|RemoveBot", re.IGNORECASE),
    ],
    "replay": [
        re.compile(r"\.replay\b", re.IGNORECASE),
        re.compile(r"SaveReplay|ReplayPath|ReplayName", re.IGNORECASE),
    ],
    "rank_progression": [
        re.compile(r"\bSeason\b", re.IGNORECASE),
        re.compile(r"\bReward\b.*\bLevel\b", re.IGNORECASE),
        re.compile(r"\bTrophy\b", re.IGNORECASE),
    ],
    "build_version": [
        re.compile(r"\bBuild\b.*\bVersion\b", re.IGNORECASE),
        re.compile(r"\bChangelist\b", re.IGNORECASE),
        re.compile(r"^Init: Version", re.IGNORECASE),
    ],
}


def default_log_path() -> Path:
    return (
        Path.home()
        / "Documents"
        / "My Games"
        / "Rocket League"
        / "TAGame"
        / "Logs"
        / "Launch.log"
    )


def iter_lines(path: Path, tail_bytes: int | None) -> list[str]:
    size = path.stat().st_size
    start = 0
    if tail_bytes is not None and tail_bytes < size:
        start = size - tail_bytes
    with path.open("rb") as fp:
        if start:
            fp.seek(start)
            fp.readline()  # discard partial line
        return [
            ln.decode("utf-8", errors="replace").rstrip("\r\n")
            for ln in fp.readlines()
        ]


def categorize(lines: list[str]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = defaultdict(list)
    seen_per_cat: dict[str, set[str]] = defaultdict(set)
    for line in lines:
        for cat, patterns in CATEGORIES.items():
            if any(p.search(line) for p in patterns):
                # Use a normalized key to dedupe by shape, not exact value.
                key = re.sub(r"\d+", "<N>", line)
                key = re.sub(r"\s+", " ", key).strip()[:160]
                if key in seen_per_cat[cat]:
                    continue
                seen_per_cat[cat].add(key)
                buckets[cat].append(line.strip())
    return buckets


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", type=Path, default=None, help="Path to Launch.log")
    ap.add_argument(
        "--max-per-category",
        type=int,
        default=8,
        help="Distinct sample lines to print per category (default 8).",
    )
    ap.add_argument(
        "--tail",
        type=int,
        default=None,
        metavar="BYTES",
        help="Only scan the last N bytes (faster on huge logs).",
    )
    args = ap.parse_args(argv)

    path = args.log or default_log_path()
    if not path.exists():
        print(f"[error] Launch.log not found at {path}", file=sys.stderr)
        return 2

    print(f"[info] scanning {path} ({path.stat().st_size:,} bytes)")
    lines = iter_lines(path, args.tail)
    print(f"[info] read {len(lines):,} lines\n")

    buckets = categorize(lines)
    if not buckets:
        print("(no categorized matches found)")
        return 0

    width = max(len(c) for c in CATEGORIES)
    for cat in CATEGORIES:
        samples = buckets.get(cat, [])
        print(f"== {cat:<{width}} == {len(samples)} distinct sample(s)")
        for s in samples[: args.max_per_category]:
            # Trim very long lines so the output stays readable.
            print(f"  {s[:300]}")
        if len(samples) > args.max_per_category:
            print(f"  ... +{len(samples) - args.max_per_category} more")
        print()

    print("[hint] If 'mmr_skill' is empty after a ranked match + RL restart,")
    print("       MMR likely isn't logged in your build — external API would")
    print("       be the only path. If it has hits, paste a couple of lines")
    print("       and we'll write parsers in log_watcher.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
