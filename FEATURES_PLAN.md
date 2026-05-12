# Feature: Per-Match MMR Delta Collection

This document describes how RLPeak collects the MMR (Matchmaking Rating) delta for each Rocket League ranked match, as an implementation guide for porting the feature to a codebase with a different structure.

The goal of the feature: while the user plays Rocket League, surface (a) the per-playlist MMR change since the session started, (b) the total MMR change across all ranked playlists, and (c) the number of ranked matches played in that session — refreshing automatically as new matches complete.

---

## 1. Files in this repository

### Rust (backend — Tauri command + worker thread)
- `src-tauri/src/win_loss_overlay.rs` — single file that owns the entire MMR-delta pipeline:
  - Constants for tracker hosts, ranked playlist IDs, poll intervals, timeouts, retry schedule.
  - `TrackerPlayer`, `TrackerSnapshot`, `MmrSnapshotState`, `WinLossOverlayMmrPlaylistState`, `WinLossOverlayRuntimeState` structs.
  - Player detection by parsing Rocket League's `Launch.log` (`detect_player_from_log`, `latest_launch_log`, `is_launch_log_current_for_running_session`).
  - HTTP fetch from tracker.gg with rotating User-Agent / TLS-emulation profiles (`fetch_tracker_data`, `fetch_tracker_data_with_reqwest_profile`, `fetch_tracker_data_with_wreq_profile`).
  - JSON parsing into per-playlist ratings (`extract_tracker_stats`).
  - Baseline/current snapshot bookkeeping and delta computation (`apply_mmr_baseline`, `update_mmr_runtime_state`, `build_mmr_breakdown`, `total_mmr`).
  - Long-running worker (`run_mmr_worker`) and failure classification (`classify_tracker_http_failure`, `MmrFailureReason`).
  - Emits state to the frontend via the Tauri event `plugins://win-loss-overlay/state`.

### TypeScript (frontend bridge + UI)
- `src/modules/plugins/winLossOverlayRuntimeService.ts` — typed wrapper around the Tauri `invoke` / `listen` API. Defines `WinLossOverlayRuntimeState` and `WinLossOverlayMmrPlaylistState`, parses/validates the payload, and exposes `listenWinLossOverlayRuntimeState`, `getWinLossOverlayRuntimeState`, `resetWinLossOverlaySession`, etc.
- `src/ui/components/WinLossOverlayThemePanel.tsx` — renders `mmr_delta` (with sign and color) in the overlay window.
- `src/ui/pages/winLossOverlayPageSelectors.ts` — derives display strings (`+12`, `-7`, total/per-playlist breakdowns) from runtime state.
- `src/ui/pages/PluginDetailPage.tsx` — the plugin's "Win/Loss Overlay" detail page that surfaces MMR status, failure reasons, and a reset action.

---

## 2. Core logic / approach

The pipeline is a **baseline + current snapshot** model run on a background worker:

1. **Detect the local player.** Parse Rocket League's `Launch.log` for a line that reveals the active account's platform (`steam` or `epic`) plus either the Steam ID or Epic display name. Verify the log file's freshness against the running Rocket League process so a stale log from a previous user isn't used.
2. **Fetch a snapshot from tracker.gg.** Build a profile URL: `api.tracker.gg/api/v2/rocket-league/standard/profile/{steam|epic}/{id-or-name}`. Before hitting the API, issue a "warmup" GET against `rocketleague.tracker.network` so the response cookies/anti-bot challenge succeed. Send the API request with browser-like headers (Origin/Referer to `rocketleague.tracker.network`). If the default `reqwest` client gets blocked, fall back to a TLS-fingerprint emulator (`wreq`) cycling through several browser profiles.
3. **Parse the JSON** into `TrackerSnapshot { playlists: HashMap<i32, { rating, matches, name, tier_name }> }`, keeping only the known ranked playlist IDs (`10, 11, 12, 13, 27, 28, 29, 30, 34, 63` — Duels / Doubles / Standard / Hoops / Rumble / Dropshot / Snow Day / Tournament playlists).
4. **Set the baseline once per session.** The first successful snapshot (after Rocket League is detected and the player is known) becomes `baseline`. Every subsequent successful snapshot becomes `current`.
5. **Compute deltas.** For each ranked playlist:
   - `delta = current.rating − baseline.rating`
   - `matches_delta = current.matches − baseline.matches`
   The **total session MMR delta** is `sum(current.rating) − sum(baseline.rating)` across ranked playlists. Persist a `last_stable_delta` so transient fetch failures don't flicker the UI back to 0.
6. **Poll loop.** Use two cadences: a fast "waiting" interval (~3 s) while looking for Rocket League / the player, and a steady interval (~30 s) once snapshots are flowing. A retry schedule with increasing back-off handles transient HTTP failures.
7. **Reset on session end / user action.** When Rocket League exits, or the user clicks "Reset session," clear baseline + current and re-arm the loop. A "new ranked match detected" check (`snapshot_has_new_ranked_match`) compares match counts so the UI can flag fresh results.
8. **Push state to the UI.** Every state change emits a single serialized struct (`WinLossOverlayRuntimeState`) over a Tauri event channel; the React UI listens and re-renders.

Status machine for the MMR side (`loading → ready → syncing → synced` on success, `failed` with a typed `failure_reason` on error, `disabled` if turned off). Failure reasons are explicit and user-actionable: `player_not_detected`, `tracker_blocked`, `rate_limited`, `tracker_unavailable`, `profile_private_or_missing`, `non_json_response`, `parse_failed`, `no_ranked_stats`, `network_error`, `unknown`.

---

## 3. Dependencies and external calls

### External HTTP
- `GET https://rocketleague.tracker.network/rocket-league/profile/{platform}/{id}/overview` — warmup (cookies / anti-bot).
- `GET https://api.tracker.gg/api/v2/rocket-league/standard/profile/{platform}/{id}` — the JSON snapshot.
- Requests sent with browser User-Agent, `Origin` and `Referer` headers pointing at `rocketleague.tracker.network`, 20 s timeout. Steam IDs and Epic display names are URL-encoded.

### Rust crates
- `reqwest` — primary HTTPS client.
- `wreq` (optional feature `mmr-wreq`) — TLS/JA3 fingerprint emulation as a Chrome/Firefox/Edge browser, used to bypass anti-bot blocks when `reqwest` is rejected.
- `serde` / `serde_json` — JSON parsing.
- `urlencoding` — encoding player identifiers.
- `regex` — parsing player identity out of `Launch.log`.
- `tauri` — event emission, command registration, app handle for shared state.
- Standard `std::thread`, `std::sync::mpsc`, `std::time::Duration` for the worker.

### Local filesystem
- Rocket League install path (provided by the user in Settings) — used to locate `TAGame/Logs/Launch.log`.
- A runtime log file under the app's data root (`plugins/runtime/win_loss_overlay/logs/`) for diagnostics.

### Frontend
- `@tauri-apps/api/core` (`invoke`) and `@tauri-apps/api/event` (`listen`) — only IPC surface used by the UI.

---

## 4. Integration with the rest of the app

- **Tauri commands** registered for the runtime: `get_win_loss_overlay_runtime_state`, `start_win_loss_overlay_runtime`, `stop_win_loss_overlay_runtime`, `force_stop_win_loss_overlay_runtime`, `reset_win_loss_overlay_session`, plus overlay window controls. The MMR worker is spawned when the runtime starts and joined when it stops.
- **Event channel**: `plugins://win-loss-overlay/state` carries the full `WinLossOverlayRuntimeState` (wins/losses + MMR fields) on every change. UI subscribes via `listenWinLossOverlayRuntimeState`.
- **Settings dependency**: the user's Rocket League folder path comes from `rocketLeaguePathService`. Without it, player detection fails with `player_not_detected` and the UI prompts the user to set the path.
- **Process detection** uses `rocketLeagueProcessService` to know whether RL is running, which gates baseline acquisition.
- **UI surface**:
  - Plugin detail page (`PluginDetailPage`) shows MMR status, failure reason text, and a "Reset session" button.
  - The in-game overlay window (`WinLossOverlayThemePanel`) renders the live total MMR delta with sign + color and optionally the per-playlist breakdown.
- **Testing**: `winLossOverlayRuntimeService.test.ts` covers payload validation; component tests assert delta formatting for positive/negative/zero values; integration tests fake the runtime state to exercise the page.

---

## 5. Porting checklist for a different codebase

Adapt the pieces above to the target stack. The shape of the feature is independent of Tauri/React:

1. **Identity discovery.** Pick a source of truth for "which account is currently playing." For Rocket League the only reliable local source is `Launch.log`; on other platforms it could be a saved profile, an SSO token, or an API call. Validate freshness so you don't pick up a previous user.
2. **Snapshot fetcher.** Wrap the tracker.gg endpoints (or the equivalent stats provider) in a function that returns `{ playlists: { id → { rating, matches, name, tier } } }`. Include the warmup request, browser-like headers, and a TLS-fingerprint fallback if you see HTTP 403/451/Cloudflare interstitials.
3. **State container.** Hold three things: `baseline` (first successful snapshot), `current` (latest snapshot), `last_stable_delta` (so the UI doesn't flicker during transient failures). Add a typed `status` enum and a typed `failure_reason` enum.
4. **Delta computation.** Per playlist: `current.rating − baseline.rating` and `current.matches − baseline.matches`. Total: sum over the ranked playlist allow-list. Reuse the same playlist-ID allow-list (`10, 11, 12, 13, 27, 28, 29, 30, 34, 63`) unless you have a reason to deviate.
5. **Worker loop.** Poll fast while waiting for the game / player (~3 s), slow once steady (~30 s), retry with back-off on transient failures, reset on game exit. Decouple the loop from the UI via a message bus (events, observables, a store — whichever the target stack uses).
6. **UI bindings.** Two views: a configuration/status panel (status + failure reason + reset button) and a live display (signed delta with color). Drive both from the same emitted state object.
7. **Diagnostics.** Append structured lines (`mmr_warmup_result`, `mmr_api_result`, `mmr_baseline_failed reason=…`) to a rolling log file. These have been essential for triaging tracker.gg blocking.
8. **Reset semantics.** Provide an explicit user action to clear baseline + current. Also auto-reset when the game process disappears so a new session starts cleanly.

The end-to-end contract for the UI is small: a single struct/event carrying `{ mmr_delta, mmr_total_start, mmr_total_current, mmr_by_playlist, mmr_status, mmr_failure_reason, mmr_player_platform, mmr_http_client }`. Anything that produces that contract is a drop-in replacement for the Rust worker described here.
