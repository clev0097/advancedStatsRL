from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import requests

from .mmr_identity import PlayerIdentity


# Per FEATURES_PLAN.md §2.3 / §5.4: ranked playlist allow-list.
RANKED_PLAYLIST_IDS: frozenset[int] = frozenset(
    {10, 11, 12, 13, 27, 28, 29, 30, 34, 63}
)

PLAYLIST_DISPLAY_NAMES: dict[int, str] = {
    10: "Ranked Duel",
    11: "Ranked Doubles",
    12: "Ranked Solo Standard",
    13: "Ranked Standard",
    27: "Hoops",
    28: "Rumble",
    29: "Dropshot",
    30: "Snow Day",
    34: "Tournament",
    63: "Ranked",
}

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_TRACKER_NETWORK_BASE = "https://rocketleague.tracker.network"
_TRACKER_API_BASE = "https://api.tracker.gg"

_DEFAULT_TIMEOUT = 20.0


class MmrFailureReason(str, Enum):
    PLAYER_NOT_DETECTED = "player_not_detected"
    TRACKER_BLOCKED = "tracker_blocked"
    RATE_LIMITED = "rate_limited"
    TRACKER_UNAVAILABLE = "tracker_unavailable"
    PROFILE_PRIVATE_OR_MISSING = "profile_private_or_missing"
    NON_JSON_RESPONSE = "non_json_response"
    PARSE_FAILED = "parse_failed"
    NO_RANKED_STATS = "no_ranked_stats"
    NETWORK_ERROR = "network_error"
    UNKNOWN = "unknown"


class MmrFetchError(Exception):
    def __init__(self, reason: MmrFailureReason, message: str = "") -> None:
        super().__init__(message or reason.value)
        self.reason = reason


@dataclass(frozen=True)
class PlaylistRating:
    playlist_id: int
    rating: int
    matches: int
    name: str | None = None
    tier_name: str | None = None


@dataclass
class TrackerSnapshot:
    playlists: dict[int, PlaylistRating] = field(default_factory=dict)

    def total_rating(self) -> int:
        return sum(p.rating for p in self.playlists.values())

    def total_matches(self) -> int:
        return sum(p.matches for p in self.playlists.values())


def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": _DEFAULT_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return s


def _encode(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _profile_warmup_url(identity: PlayerIdentity) -> str:
    return (
        f"{_TRACKER_NETWORK_BASE}/rocket-league/profile/"
        f"{_encode(identity.platform)}/{_encode(identity.id_or_name)}/overview"
    )


def _profile_api_url(identity: PlayerIdentity) -> str:
    return (
        f"{_TRACKER_API_BASE}/api/v2/rocket-league/standard/profile/"
        f"{_encode(identity.platform)}/{_encode(identity.id_or_name)}"
    )


def classify_failure(exc: BaseException | None, status: int | None) -> MmrFailureReason:
    if isinstance(exc, requests.exceptions.Timeout):
        return MmrFailureReason.NETWORK_ERROR
    if isinstance(exc, requests.exceptions.ConnectionError):
        return MmrFailureReason.NETWORK_ERROR
    if status is not None:
        if status in (401, 403, 451):
            return MmrFailureReason.TRACKER_BLOCKED
        if status == 404:
            return MmrFailureReason.PROFILE_PRIVATE_OR_MISSING
        if status == 429:
            return MmrFailureReason.RATE_LIMITED
        if 500 <= status < 600:
            return MmrFailureReason.TRACKER_UNAVAILABLE
    if isinstance(exc, requests.exceptions.RequestException):
        return MmrFailureReason.NETWORK_ERROR
    return MmrFailureReason.UNKNOWN


def _api_headers(identity: PlayerIdentity) -> dict[str, str]:
    referer = (
        f"{_TRACKER_NETWORK_BASE}/rocket-league/profile/"
        f"{_encode(identity.platform)}/{_encode(identity.id_or_name)}/overview"
    )
    return {"Origin": _TRACKER_NETWORK_BASE, "Referer": referer}


def parse_tracker_payload(payload: Any) -> TrackerSnapshot:
    """Parse the tracker.gg ``/profile`` response into a ``TrackerSnapshot``.

    Raises ``MmrFetchError`` with a specific reason if parsing fails or the
    profile contains no ranked stats.
    """

    if not isinstance(payload, dict):
        raise MmrFetchError(MmrFailureReason.PARSE_FAILED, "payload not a dict")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise MmrFetchError(MmrFailureReason.PARSE_FAILED, "missing data")
    segments = data.get("segments")
    if not isinstance(segments, list):
        raise MmrFetchError(MmrFailureReason.PARSE_FAILED, "missing segments")

    out: dict[int, PlaylistRating] = {}
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") != "playlist":
            continue
        attrs = seg.get("attributes") or {}
        try:
            pid = int(attrs.get("playlistId"))
        except (TypeError, ValueError):
            continue
        if pid not in RANKED_PLAYLIST_IDS:
            continue
        stats = seg.get("stats") or {}
        rating_block = stats.get("rating") or {}
        matches_block = stats.get("matchesPlayed") or {}
        tier_block = stats.get("tier") or {}
        try:
            rating = int(rating_block.get("value"))
        except (TypeError, ValueError):
            continue
        try:
            matches = int(matches_block.get("value") or 0)
        except (TypeError, ValueError):
            matches = 0
        metadata = seg.get("metadata") or {}
        name = metadata.get("name") if isinstance(metadata, dict) else None
        tier_meta = tier_block.get("metadata") if isinstance(tier_block, dict) else None
        tier_name = tier_meta.get("name") if isinstance(tier_meta, dict) else None
        out[pid] = PlaylistRating(
            playlist_id=pid,
            rating=rating,
            matches=matches,
            name=name if isinstance(name, str) else PLAYLIST_DISPLAY_NAMES.get(pid),
            tier_name=tier_name if isinstance(tier_name, str) else None,
        )

    if not out:
        raise MmrFetchError(MmrFailureReason.NO_RANKED_STATS, "no ranked segments")
    return TrackerSnapshot(playlists=out)


def fetch_tracker_snapshot(
    identity: PlayerIdentity,
    session: requests.Session | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> TrackerSnapshot:
    """Fetch a fresh ``TrackerSnapshot`` for ``identity``.

    Raises ``MmrFetchError`` on any non-success path. The caller is expected
    to map the failure reason into UI state.
    """

    s = session or _build_session()
    # Warmup — sets anti-bot / Cloudflare cookies on the session.
    try:
        s.get(_profile_warmup_url(identity), timeout=timeout)
    except requests.exceptions.RequestException:
        # Warmup failures aren't fatal; the API call may still succeed.
        pass

    url = _profile_api_url(identity)
    try:
        resp = s.get(url, headers=_api_headers(identity), timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise MmrFetchError(classify_failure(e, None), str(e)) from e

    if resp.status_code != 200:
        raise MmrFetchError(
            classify_failure(None, resp.status_code),
            f"HTTP {resp.status_code}",
        )

    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "json" not in ctype:
        raise MmrFetchError(
            MmrFailureReason.NON_JSON_RESPONSE,
            f"unexpected content-type: {ctype!r}",
        )

    try:
        payload = resp.json()
    except ValueError as e:
        raise MmrFetchError(MmrFailureReason.NON_JSON_RESPONSE, str(e)) from e

    return parse_tracker_payload(payload)
