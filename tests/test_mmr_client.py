from __future__ import annotations

import pytest
import requests

from rl_tracker.mmr_client import (
    MmrFailureReason,
    MmrFetchError,
    RANKED_PLAYLIST_IDS,
    classify_failure,
    parse_tracker_payload,
)


def _segment(playlist_id: int, rating: int, matches: int, name: str = "X", tier: str = "Gold I"):
    return {
        "type": "playlist",
        "attributes": {"playlistId": playlist_id},
        "metadata": {"name": name},
        "stats": {
            "rating": {"value": rating},
            "matchesPlayed": {"value": matches},
            "tier": {"metadata": {"name": tier}},
        },
    }


def test_parse_filters_to_ranked_playlists() -> None:
    payload = {
        "data": {
            "segments": [
                _segment(11, 900, 50, "Doubles"),
                _segment(13, 1100, 80, "Standard"),
                _segment(1, 500, 5, "Casual"),  # not ranked, drop
                {"type": "overview"},
            ]
        }
    }
    snap = parse_tracker_payload(payload)
    assert set(snap.playlists.keys()) == {11, 13}
    assert all(pid in RANKED_PLAYLIST_IDS for pid in snap.playlists.keys())
    assert snap.playlists[11].rating == 900
    assert snap.playlists[13].matches == 80
    assert snap.total_rating() == 2000


def test_parse_no_ranked_raises() -> None:
    payload = {"data": {"segments": [_segment(1, 100, 1, "Casual")]}}
    with pytest.raises(MmrFetchError) as ei:
        parse_tracker_payload(payload)
    assert ei.value.reason == MmrFailureReason.NO_RANKED_STATS


def test_parse_malformed_raises_parse_failed() -> None:
    with pytest.raises(MmrFetchError) as ei:
        parse_tracker_payload({"data": {}})
    assert ei.value.reason == MmrFailureReason.PARSE_FAILED


@pytest.mark.parametrize(
    "status,expected",
    [
        (403, MmrFailureReason.TRACKER_BLOCKED),
        (451, MmrFailureReason.TRACKER_BLOCKED),
        (404, MmrFailureReason.PROFILE_PRIVATE_OR_MISSING),
        (429, MmrFailureReason.RATE_LIMITED),
        (500, MmrFailureReason.TRACKER_UNAVAILABLE),
        (503, MmrFailureReason.TRACKER_UNAVAILABLE),
    ],
)
def test_classify_failure_by_status(status: int, expected: MmrFailureReason) -> None:
    assert classify_failure(None, status) == expected


def test_classify_failure_timeout() -> None:
    exc = requests.exceptions.Timeout("slow")
    assert classify_failure(exc, None) == MmrFailureReason.NETWORK_ERROR


def test_classify_failure_connection_error() -> None:
    exc = requests.exceptions.ConnectionError("boom")
    assert classify_failure(exc, None) == MmrFailureReason.NETWORK_ERROR
