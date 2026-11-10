# Changelog

All notable changes to cosine-digger will be documented in this file.

## [0.4.0] - Unreleased

A relaunch around your taste profile: a local app, YouTube Music playlists that refresh on Run now or on a schedule, and one fast skill.

### Added
- **Taste profile** (`profile`, `cosine_digger.profile`): artists, labels and anchor tracks weighted by dig seeds, saves, passes, dug-from events, leads and your library, with recency decay and per-source weights.
- **Daily and weekly playlists** (`playlist daily|weekly|due`): about 20 and 50 tracks from the profile, replacing private "Digger Daily" / "Digger Weekly" YouTube Music playlists; never serves owned, already-served, muted or already-known tracks; spreads picks across artists and labels; every track carries its why. `--dry-run` and `--json`.
- **Served ledger**: every track written to any playlist is recorded so no generated playlist repeats it.
- **Scheduler**: runs inside `serve` (catch-up once after a missed run, one-hour retry backoff) or natively via `schedule install` (launchd on macOS, crontab line on Linux).
- **Local app** (`serve`): taste map with click-to-dig and live branches, playlists, setup page; FastAPI on 127.0.0.1, no Node build.
- **`quick`**: seeds in, explained picks and a verified private playlist out in seconds; `--deep` follows labels and artists one hop (Discogs responses cached, so repeats are fast).
- **Fresh finds** (optional `EXA_API_KEY`): bounded web searches for new releases near your taste, kept only when they resolve to a real cosine.club or YouTube Music track.
- **`cosine_digger.api`**: console-free application API (resolve seeds, dig, chain, export, `import_library`) used by the CLI, the app, the skill and other programs.
- **Config and `doctor`**: one settings home (`COSINE_DIGGER_HOME`, default `~/.cosine-digger`), private `settings.json`, every setting in `.env.example`; `doctor [--json]` checks keys, YouTube login, music folder, Exa key and DB schema.
- **Album seeds**: "Artist - Album" resolves through Discogs release search when cosine.club search has no evidence.
- `/dig` skill (`skills/dig/SKILL.md`).
- Packaging: wheel with the app's static files, Docker image and compose file, CI workflow.
- **`start`, `status`, `stop`**: `start` runs Digger in the background on 127.0.0.1, opens your browser and keeps running after Terminal closes (it reads the `.env` in the folder you run it from, as `serve` does; the in-app schedule runs only with `--scheduler`, which `start` remembers); `status` says whether it's running, its address and its data folders; `stop` waits for running jobs and keeps your digs and settings. `stop` only ever stops a Digger that `start` launched and can confirm; `start` reuses a running one and remembers the port.
- **Installer** (`scripts/start.sh`, published as a release asset): installs uv if needed, Python 3.12 and this release without sudo, adds the command folder to your PATH only when needed and says so, then runs `cosine-digger start`.
- **Release workflow** (`release.yml`, on a version tag only): builds once, tests that exact wheel and the installer, then publishes the same files to PyPI, a GitHub release and GHCR.
- **iPhone and iPad app** (`ios/`): pair with your Digger, choose seeds, audition picks in YouTube's embedded player, keep or pass with Undo, follow a pick's label, artist or credited person into a new step and come back. iPhone and narrow iPad windows keep the player above the step; wider iPad windows show the trail, the step and the player beside the pick's details. Digger on the computer stays the source of truth.
- **Companion mode** (`serve --companion`, `companion`): Digger listens on your network for the app and lets another device in only with a pairing token (`~/.cosine-digger/companion_token`, mode 0600, `--rotate` to replace it). Paired devices can use the dig routes (search, digs, steps, jobs, keeps and passes, heard), send a dig to its YouTube Music playlist, ask the computer to log in to YouTube Music from its Chrome, and read the settings without changing them. `companion` prints the pairing link with the computer's Bonjour name and LAN addresses; `--copy` copies it.
- Setup › About explains how Digger runs and where your settings and digs are; after 10 s without the server, the banner says how to start Digger again (for `serve` and Docker: the way you started it).

### Changed
- YouTube Music export now uses ytmusicapi browser-header login (`youtube login`), replaces playlists in place and verifies membership by re-reading them.
- Existing taste DBs migrate in place (versioned migrations, WAL).
- Discogs pacing waits only the time still owed since the last request; cache hits skip it.

### Removed
- Obsidian notes, Apple Music / iTunes links and the `/discover` album-set builder.
- The `/deep-seed` skill (replaced by `/dig`, with `--deep` for label and artist hops).
- The Google YouTube Data API client.
- Plex shelves (moved to their own repo, which imports cosine-digger as a library).

## [0.3.2] - 2026-04-19

### Added
- **`/discover` skill** — zone-matched music discovery from album seeds. Paste 5-15 albums, get a markdown listening note with real `music.apple.com` links on every track. Uses the free iTunes Search API (no auth, no paid Apple Developer Program account required).
  - Parallel fallback ladder per seed (direct lookup → label roster → Discogs credits → Bandcamp → exa)
  - Auto-detects the aesthetic zone from tags + label reputation; v1 ships with an `ambient` profile
  - Parallel `dig` + `label-dig` subprocesses (chain-dig and artist-dig structurally banned for this skill)
  - Six confidence gates abort loud on low-confidence input rather than producing a wrong-zone note
  - Smoke run on 5 ambient seeds: 94% real catalog-URL link rate (50 of 53 shortlist tracks)
- New module `skills/album-set-builder/album_set_builder/itunes_search.py` — async httpx client against `itunes.apple.com/search` with deterministic `best_match()` + URL sanitizer
- **`$COSINE_DIGGER_NOTES_DIR` environment variable** and **`--notes-dir` CLI flag** to configure where listening notes are written. Defaults to `~/cosine-digger-notes/`. Obsidian users point this at a folder inside their vault.

### Changed
- `/discover` slash command now invokes the new zone-matched skill. The old prose-only playbook (`skills/cosine-digger-discover/`) has been retired.
- README quickstart leads with the new `/discover` flow; `/deep-seed` stays as the power-user path.

### Removed
- `skills/cosine-digger-discover/` — the old playbook-only skill. Its Discogs probe was already promoted to `src/cosine_digger/discogs_probe.py` in 0.3.1.
- AppleScript playlist creation from the discovery flow. The iTunes Store trackId and the Music.app library database ID are different namespaces, so the old "create empty playlist + try to add tracks" loop was never going to work without MusicKit ($99/yr) — it only produced an empty placeholder. The Obsidian note with tappable Apple Music links is now the sole output.
- Gate G5a (AppleScript bridge exit) — obsolete now that the skill makes no AppleScript calls. G5b (iTunes match rate ≥50%) remains.

## [0.3.1] - 2026-04-18

### Added
- `--json` flag on `dig`, `label-dig`, `search`, and `lookup` for subprocess-friendly structured output
- `cosine_digger.discogs_probe` module — promoted from the discover skill so multiple skills can share it
  - New invocation: `uv run python -m cosine_digger.discogs_probe <url> [--limit N] [--with-cosine]`
  - Old path (`skills/cosine-digger-discover/scripts/discogs_probe.py`) replaced with a migration stub

### Changed
- In `--json` mode, Rich progress output is routed to stderr so stdout is pure JSON suitable for subprocess consumers
- `--json` and `--output` are mutually exclusive (exit code 2 if both provided)

## [0.3.0] - 2026-03-31

### Changed
- **RRF is now the default scorer.** All dig commands use Reciprocal Rank Fusion by default, making cross-era and cross-format digging more robust (vinyl-sourced tracks no longer get buried by score miscalibration)
- Renamed "default" scorer to "consensus" (`--scorer consensus`) for clarity
- `--scorer rrf` is no longer needed — it's the default. Use `--scorer consensus` for the old behavior

## [0.2.0] - 2026-03-29

### Added
- Pluggable scorer registry with three strategies: `default`, `adventurous`, and `rrf`
- `--scorer` flag on all 7 dig commands (`dig`, `playlist-dig`, `label-dig`, `artist-dig`, `producer-dig`, `chain-dig`, `taste-dig`)
- Serendipity metric displayed in result tables (measures how surprising each result is)
- Adventurous scorer rewards score spread across seeds (finds tracks one seed loves but others don't)
- RRF (Reciprocal Rank Fusion) scorer uses pure rank-based fusion, ignoring raw similarity scores

### Changed
- Scorer normalization moved from per-scorer to per-call-site, fixing cross-branch score comparability in chain-dig
- Score display now always uses `final_score` regardless of fusion mode
- RRF fallback rank uses actual result set size instead of magic constant
- Duplicate track IDs in seed rank lists are now deduplicated

### Fixed
- Chain-dig `_merge_results` now correctly compares raw scores across branches before normalizing for display
- Removed dead `scorer` and `mode` parameters from render functions
