from __future__ import annotations

import json
import sys
from pathlib import Path
import re
from typing import Literal

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from .client import CosineClient, CosineClientError, CosineHTTPError
from .config import ensure_env, load_config
from .discogs import (
    fetch_label_info,
    fetch_label_releases,
    extract_label_id,
    extract_artist_id,
    fetch_release_credits,
    fetch_artist_releases,
    fetch_artist_info,
    fetch_release_artists,
    DEFAULT_PRODUCER_ROLES,
    DiscogsError,
)
from .fusion import SCORERS, fuse_similar_tracks
from .models import Track

app = typer.Typer(help="Local-first music discovery: a taste profile, self-refreshing YouTube Music playlists and a retro taste-map app, built on cosine.club.", pretty_exceptions_show_locals=False)
console = Console()


@app.callback()
def configure(ctx: typer.Context):
    """Load shared settings before running a command."""
    from .runtime import LIFECYCLE_COMMANDS

    # Every command reads this folder's .env without replacing environment values.
    # start/status/stop keep saved keys out of their environment and the server start launches.
    lifecycle = ctx.invoked_subcommand in LIFECYCLE_COMMANDS
    try:
        ctx.obj = load_config()
    except ValueError as exc:
        from .api import scrub

        typer.echo(f"Invalid setting: {scrub(str(exc))}", err=True)
        raise typer.Exit(2) from None
    if not lifecycle:
        ensure_env(ctx.obj)


def _validate_scorer(value: str) -> str:
    if value not in SCORERS:
        raise typer.BadParameter(f"Unknown scorer: {value!r}. Choose from: {', '.join(SCORERS)}")
    return value


from . import seeds as _seeds
from .seeds import _artist_matches, _is_track_not_found, _track_title


def _fallback_discogs_search(seed: str, client: CosineClient, limit: int = 5) -> Track | None:
    return _seeds._fallback_discogs_search(seed, client, limit, notify=console.print)


def resolve_seed(seed: str, client: CosineClient, max_per_release: int = 5) -> list[Track]:
    """Resolve one seed, printing progress to the console."""
    return _seeds.resolve_seed(seed, client, max_per_release=max_per_release, notify=console.print)


def _normalize_artist(name: str) -> str:
    """Normalize artist name for fuzzy matching — strips (2), (3) etc.

    Note: taste.py has a public `normalize_artist()` with whitespace collapse.
    This private version is kept for backward compat in existing CLI commands.
    New code should use `from .taste import normalize_artist`.
    """
    return re.sub(r"\s*\(\d+\)\s*$", "", name.strip().lower())


def _is_various_artists(name: str) -> bool:
    """Treat Various Artists compilations as ungrouped for diversity limits."""
    normalized = _normalize_artist(name)
    return normalized in ("various", "various artists", "va")


def _get_artist_group(name: str, known_artists: dict[str, int]) -> str | None:
    """Find which known artist group this name belongs to.

    Uses a fuzzy prefix match so minor variants group together, but leaves
    compilation credits ungrouped because they usually contain many artists.
    """
    if _is_various_artists(name):
        return None

    normalized = _normalize_artist(name)
    for known in known_artists:
        if _is_various_artists(known):
            continue
        if normalized.startswith(known) or known.startswith(normalized):
            return known
    return None


def _youtube_cell(track: Track) -> str:
    """Return a short but complete youtu.be URL the terminal can detect for cmd-click."""
    if track.video_id:
        return f"https://youtu.be/{track.video_id}"
    if track.video_uri:
        return track.video_uri
    return ""


def _seed_cell(track: Track) -> str:
    """Format as 'Artist - Track' for easy copy-paste as a seed.

    Escaped for Rich: unescaped, "[b side]" in a title is read as a style tag and
    vanishes, and "[/remix]" raises MarkupError."""
    return escape(f"{track.artist} - {_track_title(track)}")


def _render_dig_table(results) -> Table:
    table = Table(title="Dig Results", show_lines=True, border_style="dim")
    table.add_column("Rank", justify="right")
    table.add_column("Artist - Track")
    table.add_column("Score", justify="right")
    table.add_column("#", justify="right")
    table.add_column("Srndpt", justify="right")
    table.add_column("Listen", no_wrap=True)

    for index, item in enumerate(results, start=1):
        score = item.final_score
        link_parts = []
        yt_url = _youtube_cell(item.track)
        if yt_url:
            link_parts.append(f"[link={yt_url}]YT[/link]")
        discogs_url = item.track.external_link or ""
        if discogs_url:
            link_parts.append(f"[link={discogs_url}]Discogs[/link]")
        links = " ".join(link_parts)
        table.add_row(
            str(index),
            _seed_cell(item.track),
            f"{score:.3f}",
            str(item.match_count),
            f"{item.serendipity:.3f}",
            links,
        )

    return table


def _render_tracks_table(tracks: list[Track], title: str) -> Table:
    table = Table(title=title)
    table.add_column("Rank", justify="right")
    table.add_column("Artist - Track")
    table.add_column("ID")

    for index, track in enumerate(tracks, start=1):
        table.add_row(str(index), _seed_cell(track), track.id)

    return table


@app.command()
def dig(
    seeds: list[str] = typer.Argument(..., help="URLs or 'Artist - Track' text"),
    limit: int = typer.Option(20, "--limit", "-l", help="Max results"),
    youtube: bool = typer.Option(False, "--youtube", "-y", help="Create YouTube playlist"),
    playlist_name: str | None = typer.Option(None, "--playlist-name", "-p"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Export to JSON file"),
    json_output: bool = typer.Option(False, "--json", help="Emit results as JSON to stdout (mutually exclusive with --output)"),
    # Hidden advanced flags (continued below)
    # Note: when json_output is True we redirect rich progress to stderr so stdout stays pure JSON.
    year_min: int | None = typer.Option(None, "--year-min", hidden=True),
    year_max: int | None = typer.Option(None, "--year-max", hidden=True),
    collectors_min: int | None = typer.Option(None, "--collectors-min", hidden=True),
    collectors_max: int | None = typer.Option(None, "--collectors-max", hidden=True),
    want_min: int | None = typer.Option(None, "--want-min", hidden=True),
    want_max: int | None = typer.Option(None, "--want-max", hidden=True),
    price_min: float | None = typer.Option(None, "--price-min", hidden=True),
    price_max: float | None = typer.Option(None, "--price-max", hidden=True),
    mode: Literal["centroid", "intersection"] = typer.Option("centroid", "--mode", "-m", hidden=True),
    limit_per_seed: int = typer.Option(100, "--limit-per-seed", hidden=True),
    deep: bool = typer.Option(False, "--deep", help="Fetch 200 per seed for maximum obscurity"),
    save: str | None = typer.Option(None, "--save", "-s", help="Save results as a cosine.club playlist"),
    max_per_release: int = typer.Option(5, "--max-per-release", hidden=True, help="Max tracks per Discogs release"),
    scorer: str = typer.Option("rrf", "--scorer", callback=_validate_scorer, help=f"Scoring strategy: {', '.join(SCORERS)}"),
):
    """
    Find similar tracks from one or more seed URLs/searches.

    Seeds can be:
    - Discogs URLs: https://discogs.com/release/123
    - YouTube URLs: https://youtube.com/watch?v=abc
    - SoundCloud URLs: https://soundcloud.com/artist/track
    - Text search: "Joy Orbison - Hyph Mngo"
    """
    if json_output and output is not None:
        print('{"error": "--json and --output are mutually exclusive"}', file=sys.stderr)
        raise typer.Exit(code=2)
    original_stderr = console.stderr
    if json_output:
        console.stderr = True
    try:
        with CosineClient() as client:
            resolved_seeds: list[Track] = []
            skipped_seeds: list[str] = []

            # When multiple Discogs releases are provided, limit tracks per release
            # so each release gets proportional representation instead of one album
            # dominating the seed set. Target ~10 total seeds across all releases.
            discogs_seed_count = sum(1 for s in seeds if "discogs.com" in s.lower())
            effective_max_per_release = max_per_release
            if discogs_seed_count > 1:
                effective_max_per_release = min(max_per_release, max(1, 10 // discogs_seed_count))

            for seed in seeds:
                tracks = resolve_seed(seed, client, max_per_release=effective_max_per_release)
                if not tracks:
                    skipped_seeds.append(seed)
                    console.print(f"[yellow]Seed not found in cosine.club index: {seed} (skipped)[/]")
                    continue
                resolved_seeds.extend(tracks)

            if skipped_seeds:
                total = len(seeds)
                used = total - len(skipped_seeds)
                console.print(
                    f"[yellow]Used {used}/{total} seeds "
                    f"({len(skipped_seeds)} not in index)[/]"
                )

            if not resolved_seeds:
                console.print("[red]No seeds could be resolved.[/]")
                raise typer.Exit(code=1)

            console.print(f"\n[bold]Seeds for fusion ({len(resolved_seeds)}):[/]")
            for i, s in enumerate(resolved_seeds, 1):
                console.print(f"  {i}. {s.artist} - {_track_title(s)} [dim]({s.id})[/dim]")

            if deep:
                limit_per_seed = 200

            api_filters = {
                "start_year": year_min,
                "end_year": year_max,
                "min_have": collectors_min,
                "max_have": collectors_max,
                "min_want": want_min,
                "max_want": want_max,
                "min_price": price_min,
                "max_price": price_max,
            }
            api_filters = {key: value for key, value in api_filters.items() if value is not None}

            ranked = fuse_similar_tracks(
                [track.id for track in resolved_seeds],
                client,
                mode=mode,
                limit_per_seed=limit_per_seed,
                scorer=scorer,
                **api_filters,
            )
            ranked = ranked[:limit]

            if json_output:
                payload = {
                    "seeds": list(seeds),
                    "skipped_seeds": skipped_seeds,
                    "resolved_seeds": [t.model_dump() for t in resolved_seeds],
                    "mode": mode,
                    "limit": limit,
                    "scorer": scorer,
                    "results": [
                        {"rank": i + 1, **item.model_dump()}
                        for i, item in enumerate(ranked)
                    ],
                }
                sys.stdout.write(json.dumps(payload, default=str) + "\n")
                return

            _finish_dig(
                client=client,
                ranked=ranked,
                mode=mode,
                seeds=list(seeds),
                resolved_seeds=resolved_seeds,
                limit=limit,
                youtube=youtube,
                playlist_name=playlist_name,
                save=save,
                output=output,
            )
    except CosineClientError as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(code=1)
    finally:
        console.stderr = original_stderr


def _youtube_export(tracks: list[Track], name: str, description: str, conn=None):
    """Write a private YouTube Music playlist the way every export does
    (api.export): the same name is replaced, the served ledger learns the
    tracks, and failures print our own words, never provider text (it can
    carry the login). Returns the report, or None when nothing was written."""
    from . import api

    picks = [api.Pick(track=track, score=0.0, why="") for track in tracks]
    own = conn is None
    if own:
        conn = api.open_db()
    try:
        report = api.export(picks, name, conn=conn, description=description)
    except Exception as exc:
        console.print("[red]YouTube export failed:[/]", Text(api._export_error(exc)))
        return None
    finally:
        if own:
            conn.close()
    state = "verified" if report.verified else "NOT verified: open it to check"
    from . import youtube as _youtube

    link = _youtube.account_link(report.playlist_url, _youtube.login_account(_youtube.auth_path()))
    console.print(f"[green]Playlist:[/] {link}")
    console.print(f"{len(report.added)}/{len(report.requested)} tracks in the playlist · {state}")
    return report


def _finish_dig(
    *,
    client: CosineClient,
    ranked: list,
    mode: str,
    seeds: list[str],
    resolved_seeds: list[Track],
    limit: int,
    youtube: bool = False,
    playlist_name: str | None = None,
    save: str | None = None,
    output: Path | None = None,
    skip_table: bool = False,
) -> None:
    """Shared post-fusion logic: table, YouTube, cosine playlist, JSON export."""
    if not ranked:
        console.print("[yellow]No results found.[/]")
        return

    if not skip_table:
        console.print(_render_dig_table(ranked))

    if playlist_name:
        playlist_title = playlist_name
    else:
        artists = list(dict.fromkeys(s.artist for s in resolved_seeds))
        if len(artists) <= 3:
            playlist_title = f"Dig: {', '.join(artists)}"
        else:
            playlist_title = f"Dig: {', '.join(artists[:3])} + {len(artists) - 3} more"

    yt_playlist_id = None
    yt_playlist_url = None
    export_ok = True
    if youtube:
        video_ids = [item.track.video_id for item in ranked if item.track.video_id]
        missing_videos = len(ranked) - len(video_ids)
        if missing_videos:
            console.print(f"[yellow]{missing_videos} result(s) have no YouTube video and will be omitted.[/]")
        if not video_ids:
            console.print("[yellow]No YouTube video IDs available to add.[/]")
        else:
            report = _youtube_export([item.track for item in ranked], playlist_title, "Generated by cosine-digger")
            export_ok = bool(report and report.verified)
            if report:
                yt_playlist_id, yt_playlist_url = report.playlist_id, report.playlist_url

    cosine_playlist_id = None
    cosine_playlist_url = None
    if save:
        try:
            playlist = client.create_playlist(save)
            cosine_playlist_id = playlist.id
            cosine_playlist_url = playlist.url or f"https://cosine.club/playlist/{playlist.id}"
            added = 0
            for item in ranked:
                try:
                    client.add_track_to_playlist(playlist.id, item.track.id)
                    added += 1
                except CosineClientError:
                    pass
            console.print(
                f"[green]Saved playlist:[/] {cosine_playlist_url} ({added} tracks)"
            )
        except CosineClientError as exc:
            console.print(f"[yellow]Could not save playlist: {exc}[/]")

    if output:
        output_path = output.expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "seeds": seeds,
            "resolved_seeds": [track.model_dump() for track in resolved_seeds],
            "mode": mode,
            "limit": limit,
            "results": [
                {"rank": index + 1, **item.model_dump()}
                for index, item in enumerate(ranked)
            ],
        }
        if yt_playlist_id:
            payload["youtube_playlist_id"] = yt_playlist_id
            payload["youtube_playlist_url"] = yt_playlist_url
            payload["youtube_playlist_name"] = playlist_title
        if cosine_playlist_id:
            payload["cosine_playlist_id"] = cosine_playlist_id
            payload["cosine_playlist_url"] = cosine_playlist_url
        output_path.write_text(json.dumps(payload, indent=2))
        console.print(f"[green]Exported results to[/] {output_path}")
    if not export_ok:
        raise typer.Exit(code=1)


@app.command()
def playlists():
    """List your cosine.club playlists."""
    try:
        with CosineClient() as client:
            items = client.list_playlists()
        if not items:
            console.print("[yellow]No playlists found.[/]")
            return
        table = Table(title="Playlists")
        table.add_column("Name")
        table.add_column("Tracks", justify="right")
        table.add_column("Public")
        table.add_column("URL", no_wrap=True)
        for p in items:
            table.add_row(
                p.name,
                str(p.track_count),
                "yes" if p.is_public else "no",
                p.url or "",
            )
        console.print(table)
    except CosineClientError as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(code=1)


@app.command(name="playlist-dig")
def playlist_dig(
    playlist_id: str = typer.Argument(..., help="Cosine playlist ID to use as seeds"),
    limit: int = typer.Option(20, "--limit", "-l", help="Max results"),
    youtube: bool = typer.Option(False, "--youtube", "-y", help="Create YouTube playlist"),
    playlist_name: str | None = typer.Option(None, "--playlist-name", "-p"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Export to JSON file"),
    year_min: int | None = typer.Option(None, "--year-min", hidden=True),
    year_max: int | None = typer.Option(None, "--year-max", hidden=True),
    collectors_min: int | None = typer.Option(None, "--collectors-min", hidden=True),
    collectors_max: int | None = typer.Option(None, "--collectors-max", hidden=True),
    want_min: int | None = typer.Option(None, "--want-min", hidden=True),
    want_max: int | None = typer.Option(None, "--want-max", hidden=True),
    price_min: float | None = typer.Option(None, "--price-min", hidden=True),
    price_max: float | None = typer.Option(None, "--price-max", hidden=True),
    mode: Literal["centroid", "intersection"] = typer.Option("centroid", "--mode", "-m", hidden=True),
    limit_per_seed: int = typer.Option(100, "--limit-per-seed", hidden=True),
    deep: bool = typer.Option(False, "--deep", help="Fetch 200 per seed for maximum obscurity"),
    save: str | None = typer.Option(None, "--save", "-s", help="Save results as a cosine.club playlist"),
    scorer: str = typer.Option("rrf", "--scorer", callback=_validate_scorer, help=f"Scoring strategy: {', '.join(SCORERS)}"),
):
    """Dig using tracks from an existing cosine.club playlist as seeds."""
    try:
        with CosineClient() as client:
            detail = client.get_playlist(playlist_id)
            if not detail.tracks:
                console.print("[red]Playlist has no tracks.[/]")
                raise typer.Exit(code=1)

            console.print(f"[dim]Using {len(detail.tracks)} tracks from playlist '{detail.name}' as seeds[/dim]")

            if deep:
                limit_per_seed = 200

            api_filters = {
                "start_year": year_min,
                "end_year": year_max,
                "min_have": collectors_min,
                "max_have": collectors_max,
                "min_want": want_min,
                "max_want": want_max,
                "min_price": price_min,
                "max_price": price_max,
            }
            api_filters = {k: v for k, v in api_filters.items() if v is not None}

            ranked = fuse_similar_tracks(
                [t.id for t in detail.tracks],
                client,
                mode=mode,
                limit_per_seed=limit_per_seed,
                scorer=scorer,
                **api_filters,
            )
            ranked = ranked[:limit]

            _finish_dig(
                client=client,
                ranked=ranked,
                mode=mode,
                seeds=[playlist_id],
                resolved_seeds=detail.tracks,
                limit=limit,
                youtube=youtube,
                playlist_name=playlist_name,
                save=save,
                output=output,
            )
    except CosineClientError as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(code=1)


@app.command(name="label-dig")
def label_dig(
    label_url: str = typer.Argument(..., help="Discogs label URL"),
    limit: int = typer.Option(20, "--limit", "-l", help="Max results"),
    youtube: bool = typer.Option(False, "--youtube", "-y", help="Create YouTube playlist"),
    playlist_name: str | None = typer.Option(None, "--playlist-name", "-p"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Export to JSON file"),
    json_output: bool = typer.Option(False, "--json", help="Emit results as JSON to stdout (mutually exclusive with --output)"),
    year_min: int | None = typer.Option(None, "--year-min", hidden=True),
    year_max: int | None = typer.Option(None, "--year-max", hidden=True),
    collectors_min: int | None = typer.Option(None, "--collectors-min", hidden=True),
    collectors_max: int | None = typer.Option(None, "--collectors-max", hidden=True),
    want_min: int | None = typer.Option(None, "--want-min", hidden=True),
    want_max: int | None = typer.Option(None, "--want-max", hidden=True),
    price_min: float | None = typer.Option(None, "--price-min", hidden=True),
    price_max: float | None = typer.Option(None, "--price-max", hidden=True),
    mode: Literal["centroid", "intersection"] = typer.Option("centroid", "--mode", "-m", hidden=True),
    limit_per_seed: int = typer.Option(100, "--limit-per-seed", hidden=True),
    deep: bool = typer.Option(False, "--deep", help="Fetch 200 per seed for maximum obscurity"),
    save: str | None = typer.Option(None, "--save", "-s", help="Save results as a cosine.club playlist"),
    max_seeds: int = typer.Option(15, "--max-seeds", help="Max tracks to sample from label"),
    max_per_artist: int = typer.Option(2, "--max-per-artist", help="Max tracks per artist to ensure diversity"),
    include_source: bool = typer.Option(
        False, "--include-source", help="Include tracks from the source label (default: only OTHER labels)"
    ),
    scorer: str = typer.Option("rrf", "--scorer", callback=_validate_scorer, help=f"Scoring strategy: {', '.join(SCORERS)}"),
):
    """
    Dig similar tracks based on a Discogs label's catalog.

    Finds tracks similar to what's on the label, but from OTHER labels.
    Use --include-source to include the label's own tracks in results.

    Example:
        cosine-digger label-dig https://www.discogs.com/label/27576-Sound-Signature
    """
    if json_output and output is not None:
        print('{"error": "--json and --output are mutually exclusive"}', file=sys.stderr)
        raise typer.Exit(code=2)
    original_stderr = console.stderr
    if json_output:
        console.stderr = True
    try:
        label_id = extract_label_id(label_url)
        if not label_id:
            console.print(f"[red]Could not parse label ID from URL:[/] {label_url}")
            raise typer.Exit(code=1)
        # Fetch label info
        label_info = fetch_label_info(label_id)
        console.print(f"[bold]Label:[/] {label_info.name}")

        # Fetch releases from label
        console.print(f"[dim]Fetching releases...[/dim]")
        releases = fetch_label_releases(label_id, limit=50)
        if not releases:
            console.print("[red]No releases found for this label.[/]")
            raise typer.Exit(code=1)

        console.print(f"[dim]Found {len(releases)} releases, sampling tracks...[/dim]")

        with CosineClient() as client:
            # Resolve tracks from label releases with artist diversity
            resolved_seeds: list[Track] = []
            artist_counts: dict[str, int] = {}  # Track count per artist group

            for release in releases:
                if len(resolved_seeds) >= max_seeds:
                    break

                # Build search query from release
                query = f"{release.artist} {release.title}".strip()
                if not query:
                    continue

                try:
                    results = client.search(query, limit=3)
                except CosineHTTPError:
                    continue

                if not results:
                    continue

                # Find best match (prefer artist match, enforce diversity)
                match = None
                selected_group_key: str | None = None
                for r in results:
                    if _artist_matches(release.artist, r.artist):
                        # Skip if we already have a track with this ID
                        if r.id in {s.id for s in resolved_seeds}:
                            continue

                        # Check artist diversity limit with fuzzy matching
                        normalized = _normalize_artist(r.artist)
                        group_key = _get_artist_group(r.artist, artist_counts)
                        selected_group_key = group_key or (
                            None if _is_various_artists(r.artist) else normalized
                        )

                        if selected_group_key and artist_counts.get(selected_group_key, 0) >= max_per_artist:
                            continue

                        match = r
                        break

                if match:
                    resolved_seeds.append(match)
                    if selected_group_key:
                        artist_counts[selected_group_key] = artist_counts.get(selected_group_key, 0) + 1
                    console.print(f"[dim]  Resolved: {match.artist} - {_track_title(match)}[/dim]")

            if not resolved_seeds:
                console.print("[red]Could not resolve any tracks from this label in cosine.club.[/]")
                raise typer.Exit(code=1)

            console.print(f"\n[bold]Using {len(resolved_seeds)} tracks as seeds[/]")

            if deep:
                limit_per_seed = 200

            api_filters = {
                "start_year": year_min,
                "end_year": year_max,
                "min_have": collectors_min,
                "max_have": collectors_max,
                "min_want": want_min,
                "max_want": want_max,
                "min_price": price_min,
                "max_price": price_max,
            }
            api_filters = {k: v for k, v in api_filters.items() if v is not None}

            ranked = fuse_similar_tracks(
                [track.id for track in resolved_seeds],
                client,
                mode=mode,
                limit_per_seed=limit_per_seed,
                scorer=scorer,
                **api_filters,
            )

            # Filter out tracks by seed artists (unless --include-source)
            # This ensures results are from artists NOT on the source label
            if not include_source:
                seed_artists = {s.artist.strip().lower() for s in resolved_seeds}
                original_count = len(ranked)
                ranked = [
                    item for item in ranked
                    if item.track.artist.strip().lower() not in seed_artists
                ]
                filtered_count = original_count - len(ranked)
                if filtered_count > 0:
                    console.print(f"[dim]Filtered {filtered_count} tracks from label artists[/dim]")

            ranked = ranked[:limit]

            if json_output:
                payload = {
                    "label": label_info.name,
                    "label_id": label_id,
                    "label_url": label_url,
                    "seeds": [t.model_dump() for t in resolved_seeds],
                    "mode": mode,
                    "limit": limit,
                    "scorer": scorer,
                    "results": [
                        {"rank": i + 1, **item.model_dump()}
                        for i, item in enumerate(ranked)
                    ],
                }
                sys.stdout.write(json.dumps(payload, default=str) + "\n")
                return

            _finish_dig(
                client=client,
                ranked=ranked,
                mode=mode,
                seeds=[label_url],
                resolved_seeds=resolved_seeds,
                limit=limit,
                youtube=youtube,
                playlist_name=playlist_name or f"Label Dig: {label_info.name}",
                save=save,
                output=output,
            )

    except (CosineClientError, DiscogsError) as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(code=1)
    finally:
        console.stderr = original_stderr


@app.command(name="artist-dig")
def artist_dig(
    url: str = typer.Argument(..., help="Discogs artist or release URL"),
    limit: int = typer.Option(20, "--limit", "-l", help="Max results"),
    youtube: bool = typer.Option(False, "--youtube", "-y", help="Create YouTube playlist"),
    playlist_name: str | None = typer.Option(None, "--playlist-name", "-p"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Export to JSON file"),
    json_output: bool = typer.Option(False, "--json", help="Print results as JSON to stdout"),
    year_min: int | None = typer.Option(None, "--year-min", hidden=True),
    year_max: int | None = typer.Option(None, "--year-max", hidden=True),
    collectors_min: int | None = typer.Option(None, "--collectors-min", hidden=True),
    collectors_max: int | None = typer.Option(None, "--collectors-max", hidden=True),
    want_min: int | None = typer.Option(None, "--want-min", hidden=True),
    want_max: int | None = typer.Option(None, "--want-max", hidden=True),
    price_min: float | None = typer.Option(None, "--price-min", hidden=True),
    price_max: float | None = typer.Option(None, "--price-max", hidden=True),
    mode: Literal["centroid", "intersection"] = typer.Option("centroid", "--mode", "-m", hidden=True),
    limit_per_seed: int = typer.Option(100, "--limit-per-seed", hidden=True),
    deep: bool = typer.Option(False, "--deep", help="Fetch 200 per seed for maximum obscurity"),
    save: str | None = typer.Option(None, "--save", "-s", help="Save results as a cosine.club playlist"),
    max_seeds: int = typer.Option(15, "--max-seeds", help="Max tracks to sample from artist discography"),
    max_per_artist: int = typer.Option(2, "--max-per-artist", help="Max tracks per related artist to ensure diversity"),
    scorer: str = typer.Option("rrf", "--scorer", callback=_validate_scorer, help=f"Scoring strategy: {', '.join(SCORERS)}"),
):
    """
    Dig similar tracks based on a Discogs artist's discography.

    Accepts either an artist URL or a release URL (extracts the main artist).
    Finds tracks similar to the artist's own releases, from OTHER artists.

    Examples:
        cosine-digger artist-dig https://www.discogs.com/artist/123-Rat-Heart
        cosine-digger artist-dig https://www.discogs.com/release/456-Some-Release
    """
    if json_output and output is not None:
        print('{"error": "--json and --output are mutually exclusive"}', file=sys.stderr)
        raise typer.Exit(code=2)
    if json_output:
        # Progress goes to stderr, as in dig and label-dig; put it back however the command ends.
        original_stderr = console.stderr
        console.stderr = True
        import click

        click.get_current_context().call_on_close(lambda: setattr(console, "stderr", original_stderr))

    # Determine if this is an artist URL or a release URL
    artist_id = extract_artist_id(url)
    artist_name: str | None = None

    if artist_id:
        # Direct artist URL
        try:
            info = fetch_artist_info(artist_id)
            artist_name = info.name
            console.print(f"[bold]Artist:[/] {artist_name}")
        except DiscogsError as exc:
            console.print(f"[red]Discogs error:[/] {exc}")
            raise typer.Exit(code=1)
    else:
        # Try as a release URL — extract the main artist(s)
        try:
            artists = fetch_release_artists(url)
            if not artists:
                console.print(f"[red]No artists found on release:[/] {url}")
                raise typer.Exit(code=1)
            # Use the first (primary) artist
            artist_id = artists[0].id
            artist_name = artists[0].name
            if len(artists) > 1:
                names = ", ".join(a.name for a in artists)
                console.print(f"[dim]Release has multiple artists: {names}[/dim]")
                console.print(f"[dim]Using primary artist: {artist_name}[/dim]")
            else:
                console.print(f"[bold]Artist:[/] {artist_name} [dim](from release)[/dim]")
        except DiscogsError as exc:
            console.print(f"[red]Discogs error:[/] {exc}")
            raise typer.Exit(code=1)

    if not artist_id:
        console.print(f"[red]Could not extract artist or release ID from URL:[/] {url}")
        raise typer.Exit(code=1)

    # Fetch artist's discography
    try:
        console.print(f"[dim]Fetching discography...[/dim]")
        artist_releases = fetch_artist_releases(artist_id, limit=max_seeds * 2)
        if not artist_releases:
            console.print("[red]No releases found in discography.[/]")
            raise typer.Exit(code=1)
        console.print(f"[dim]Found {len(artist_releases)} releases, resolving tracks...[/dim]")
    except DiscogsError as exc:
        console.print(f"[red]Discogs error:[/] {exc}")
        raise typer.Exit(code=1)

    try:
        with CosineClient() as client:
            resolved_seeds: list[Track] = []
            seen_ids: set[str] = set()
            related_artist_counts: dict[str, int] = {}
            source_artist_key = _normalize_artist(artist_name or "")

            for release in artist_releases:
                if len(resolved_seeds) >= max_seeds:
                    break

                query = f"{release.artist} {release.title}".strip()
                if not query:
                    continue

                try:
                    results = client.search(query, limit=3)
                except CosineHTTPError:
                    continue

                if not results:
                    continue

                # Find best match with artist validation
                for r in results:
                    if r.id in seen_ids:
                        continue
                    if _artist_matches(release.artist, r.artist):
                        normalized = _normalize_artist(r.artist)
                        is_source_artist = bool(source_artist_key) and normalized == source_artist_key
                        group_key = _get_artist_group(r.artist, related_artist_counts)
                        selected_group_key = None if is_source_artist else (
                            group_key or (None if _is_various_artists(r.artist) else normalized)
                        )

                        if selected_group_key and related_artist_counts.get(selected_group_key, 0) >= max_per_artist:
                            continue

                        resolved_seeds.append(r)
                        seen_ids.add(r.id)
                        if selected_group_key:
                            related_artist_counts[selected_group_key] = related_artist_counts.get(selected_group_key, 0) + 1
                        console.print(f"[dim]  Resolved: {r.artist} - {_track_title(r)}[/dim]")
                        break

            if not resolved_seeds:
                console.print("[red]Could not resolve any tracks from discography in cosine.club.[/]")
                raise typer.Exit(code=1)

            console.print(f"\n[bold]Using {len(resolved_seeds)} tracks as seeds[/]")

            if deep:
                limit_per_seed = 200

            api_filters = {
                "start_year": year_min,
                "end_year": year_max,
                "min_have": collectors_min,
                "max_have": collectors_max,
                "min_want": want_min,
                "max_want": want_max,
                "min_price": price_min,
                "max_price": price_max,
            }
            api_filters = {k: v for k, v in api_filters.items() if v is not None}

            ranked = fuse_similar_tracks(
                [track.id for track in resolved_seeds],
                client,
                mode=mode,
                limit_per_seed=limit_per_seed,
                scorer=scorer,
                **api_filters,
            )

            # Filter out the source artist from results (fuzzy match)
            source_artist_lower = (artist_name or "").strip().lower()
            if source_artist_lower:
                original_count = len(ranked)
                ranked = [
                    item for item in ranked
                    if not _artist_matches(source_artist_lower, item.track.artist)
                ]
                filtered_count = original_count - len(ranked)
                if filtered_count > 0:
                    console.print(f"[dim]Filtered {filtered_count} tracks from {artist_name}[/dim]")

            ranked = ranked[:limit]

            if json_output:
                payload = {
                    "artist": artist_name,
                    "artist_id": artist_id,
                    "seeds": [
                        {"id": t.id, "artist": t.artist, "track": _track_title(t)}
                        for t in resolved_seeds
                    ],
                    "results": [
                        {
                            "rank": i + 1,
                            "artist": item.track.artist,
                            "track": _track_title(item.track),
                            "score": item.final_score,
                            "match_count": item.match_count,
                            "youtube": _youtube_cell(item.track),
                            "external_link": item.track.external_link or "",
                        }
                        for i, item in enumerate(ranked)
                    ],
                }
                # Not console.print: Rich would wrap long lines and eat "[bracketed]" text.
                sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
                return

            _finish_dig(
                client=client,
                ranked=ranked,
                mode=mode,
                seeds=[url],
                resolved_seeds=resolved_seeds,
                limit=limit,
                youtube=youtube,
                playlist_name=playlist_name or f"Artist Dig: {artist_name}",
                save=save,
                output=output,
            )

    except CosineClientError as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(code=1)


@app.command(name="producer-dig")
def producer_dig(
    release_url: str = typer.Argument(..., help="Discogs release URL"),
    limit: int = typer.Option(20, "--limit", "-l", help="Max results"),
    youtube: bool = typer.Option(False, "--youtube", "-y", help="Create YouTube playlist"),
    playlist_name: str | None = typer.Option(None, "--playlist-name", "-p"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Export to JSON file"),
    # Hidden advanced filters (same as dig/label-dig)
    year_min: int | None = typer.Option(None, "--year-min", hidden=True),
    year_max: int | None = typer.Option(None, "--year-max", hidden=True),
    collectors_min: int | None = typer.Option(None, "--collectors-min", hidden=True),
    collectors_max: int | None = typer.Option(None, "--collectors-max", hidden=True),
    want_min: int | None = typer.Option(None, "--want-min", hidden=True),
    want_max: int | None = typer.Option(None, "--want-max", hidden=True),
    price_min: float | None = typer.Option(None, "--price-min", hidden=True),
    price_max: float | None = typer.Option(None, "--price-max", hidden=True),
    mode: Literal["centroid", "intersection"] = typer.Option("centroid", "--mode", "-m", hidden=True),
    limit_per_seed: int = typer.Option(100, "--limit-per-seed", hidden=True),
    deep: bool = typer.Option(False, "--deep", help="Fetch 200 per seed for maximum obscurity"),
    save: str | None = typer.Option(None, "--save", "-s", help="Save results as a cosine.club playlist"),
    # Producer-dig specific options
    roles: str | None = typer.Option(
        None, "--roles", "-r",
        help="Comma-separated roles to include (default: Producer,Mixed By,Engineered By,Mastered By,Recorded By)"
    ),
    max_seeds: int = typer.Option(15, "--max-seeds", help="Max tracks to sample from producer discographies"),
    max_per_producer: int = typer.Option(3, "--max-per-producer", help="Max tracks per producer to ensure diversity"),
    include_credited: bool = typer.Option(
        False, "--include-credited",
        help="Include tracks by the credited producers (default: only OTHER artists)"
    ),
    scorer: str = typer.Option("rrf", "--scorer", callback=_validate_scorer, help=f"Scoring strategy: {', '.join(SCORERS)}"),
):
    """
    Dig similar tracks based on a release's production credits.

    Finds tracks similar to what the producers/engineers have worked on,
    but from OTHER artists.

    Example:
        cosine-digger producer-dig https://www.discogs.com/release/123456
    """
    # Parse roles option
    if roles:
        filter_roles = [r.strip() for r in roles.split(",")]
    else:
        filter_roles = list(DEFAULT_PRODUCER_ROLES)

    # Fetch credits from release
    try:
        credits = fetch_release_credits(release_url, roles=filter_roles)
        if not credits:
            console.print("[yellow]No matching credits found on this release.[/]")
            console.print("[dim]This release may not have detailed credits, or try different --roles[/]")
            raise typer.Exit(code=1)

        console.print(f"[bold]Found {len(credits)} credited artists:[/]")
        for credit in credits:
            console.print(f"  - {credit.name} ({credit.role_normalized})")
    except DiscogsError as exc:
        console.print(f"[red]Discogs error:[/] {exc}")
        raise typer.Exit(code=1)

    # Build set of credited artist names to exclude from results.
    # This matches the CLI help text more closely than excluding resolved seed artists.
    excluded_artist_names = {_normalize_artist(credit.name) for credit in credits}

    # Fetch discographies and collect seed candidates
    console.print(f"[dim]Fetching discographies...[/dim]")
    seed_candidates: list[tuple[str, str, str]] = []  # (producer_id, producer_name, query)

    releases_per_producer = max(1, max_seeds // len(credits) + 1)

    for credit in credits:
        try:
            artist_releases = fetch_artist_releases(
                credit.id,
                limit=releases_per_producer * 2,  # Fetch extra for filtering
            )
        except DiscogsError:
            console.print(f"[yellow]Could not fetch discography for {credit.name}[/]")
            continue

        for release in artist_releases[:releases_per_producer]:
            query = f"{release.artist} {release.title}".strip()
            if query:
                seed_candidates.append((credit.id, credit.name, query))

    if not seed_candidates:
        console.print("[red]Could not fetch any releases from producer discographies.[/]")
        raise typer.Exit(code=1)

    try:
        with CosineClient() as client:
            resolved_seeds: list[Track] = []
            producer_counts: dict[str, int] = {}  # Track count per producer

            for producer_id, producer_name, query in seed_candidates:
                if len(resolved_seeds) >= max_seeds:
                    break

                # Check producer diversity limit
                if producer_counts.get(producer_id, 0) >= max_per_producer:
                    continue

                try:
                    results = client.search(query, limit=3)
                except CosineHTTPError:
                    continue

                if not results:
                    continue

                # Find best match, skip duplicates
                for r in results:
                    if r.id in {s.id for s in resolved_seeds}:
                        continue

                    resolved_seeds.append(r)
                    producer_counts[producer_id] = producer_counts.get(producer_id, 0) + 1
                    console.print(f"[dim]  Resolved: {r.artist} - {_track_title(r)}[/dim]")
                    break

            if not resolved_seeds:
                console.print("[red]Could not resolve any tracks from producer discographies.[/]")
                raise typer.Exit(code=1)

            console.print(f"\n[bold]Using {len(resolved_seeds)} tracks as seeds[/]")

            if deep:
                limit_per_seed = 200

            api_filters = {
                "start_year": year_min,
                "end_year": year_max,
                "min_have": collectors_min,
                "max_have": collectors_max,
                "min_want": want_min,
                "max_want": want_max,
                "min_price": price_min,
                "max_price": price_max,
            }
            api_filters = {k: v for k, v in api_filters.items() if v is not None}

            ranked = fuse_similar_tracks(
                [track.id for track in resolved_seeds],
                client,
                mode=mode,
                limit_per_seed=limit_per_seed,
                scorer=scorer,
                **api_filters,
            )

            # Filter out tracks by credited producers (unless --include-credited)
            if not include_credited:
                original_count = len(ranked)
                ranked = [
                    item for item in ranked
                    if _normalize_artist(item.track.artist) not in excluded_artist_names
                ]
                filtered_count = original_count - len(ranked)
                if filtered_count > 0:
                    console.print(f"[dim]Filtered {filtered_count} tracks from credited artists[/dim]")

            ranked = ranked[:limit]

            # Build playlist name from producer names
            if not playlist_name:
                producer_names = [c.name for c in credits[:3]]
                if len(credits) > 3:
                    playlist_name = f"Producer Dig: {', '.join(producer_names)} + {len(credits) - 3} more"
                else:
                    playlist_name = f"Producer Dig: {', '.join(producer_names)}"

            _finish_dig(
                client=client,
                ranked=ranked,
                mode=mode,
                seeds=[release_url],
                resolved_seeds=resolved_seeds,
                limit=limit,
                youtube=youtube,
                playlist_name=playlist_name,
                save=save,
                output=output,
            )

    except CosineClientError as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(code=1)


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(20, "--limit", "-l"),
    json_output: bool = typer.Option(False, "--json", help="Emit results as JSON to stdout"),
):
    """Search for tracks by artist/title."""
    try:
        with CosineClient() as client:
            results = client.search(query, limit=limit)
        if json_output:
            payload = {
                "query": query,
                "limit": limit,
                "results": [t.model_dump() for t in results],
            }
            print(json.dumps(payload, default=str))
            return
        if not results:
            console.print("[yellow]No results found.[/]")
            return
        console.print(_render_tracks_table(results, "Search Results"))
    except CosineClientError as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(code=1)


@app.command()
def lookup(
    url: str = typer.Argument(..., help="Discogs/YouTube/SoundCloud URL"),
    fallback: bool = typer.Option(
        True, "--fallback/--no-fallback", help="Fallback to search if lookup fails"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit results as JSON to stdout"),
):
    """Look up tracks from a URL."""
    try:
        with CosineClient() as client:
            try:
                results = client.lookup(url)
            except CosineHTTPError as exc:
                if fallback and "discogs.com" in url.lower() and _is_track_not_found(exc):
                    fallback_track = _fallback_discogs_search(url, client, limit=10)
                    if fallback_track:
                        results = [fallback_track]
                    else:
                        results = []
                else:
                    raise
        if json_output:
            payload = {
                "url": url,
                "fallback": fallback,
                "results": [t.model_dump() for t in results],
            }
            print(json.dumps(payload, default=str))
            return
        if not results:
            console.print("[yellow]No results found.[/]")
            return
        console.print(_render_tracks_table(results, "Lookup Results"))
    except CosineClientError as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Chain-dig results table (extended with Via, badges)
# ---------------------------------------------------------------------------

def _render_chain_table(results, verbose: bool = False, gap_artists: set[str] | None = None, convergence: dict[str, int] | None = None) -> Table:
    """Render chain-dig results with Via column and badges."""
    table = Table(title="Chain Dig Results", show_lines=True, border_style="dim")
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("Artist - Track", min_width=25)
    table.add_column("Via", max_width=30)
    table.add_column("Score", justify="right", width=6)
    table.add_column("Contrast", justify="right", width=8)
    if verbose:
        table.add_column("Hop", justify="right", width=3)
    table.add_column("Listen", no_wrap=True)

    gap_artists = gap_artists or set()
    convergence = convergence or {}

    for index, item in enumerate(results, start=1):
        artist_track = _seed_cell(item.track)
        # Badges
        badges = []
        if item.track.artist in gap_artists:
            badges.append("[green]\\[NEW][/green]")
        conv_count = convergence.get(item.track.id, 0)
        if conv_count >= 2:
            badges.append(f"[cyan]\\[x{conv_count}][/cyan]")
        if badges:
            artist_track += " " + " ".join(badges)

        via = item.via or "(seed similarity)"
        link_parts = []
        yt_url = _youtube_cell(item.track)
        if yt_url:
            link_parts.append(f"[link={yt_url}]YT[/link]")
        discogs_url = item.track.external_link or ""
        if discogs_url:
            link_parts.append(f"[link={discogs_url}]Discogs[/link]")
        links = " ".join(link_parts)

        row = [str(index), artist_track, via, f"{item.final_score:.3f}", f"{item.serendipity:.3f}"]
        if verbose:
            row.append(str(item.hop))
        row.append(links)
        table.add_row(*row)

    return table


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------


def _open_db(*, require: bool = True):
    """Open the taste DB with session schema migration.

    If *require* is True and the DB does not exist, prints an error and
    raises ``typer.Exit``.  Returns a ``sqlite3.Connection`` or ``None``.
    """
    import sqlite3 as _sqlite3

    db_path = load_config().db_path
    if not db_path.exists():
        if require:
            console.print("No taste DB found. Run [bold]taste-scan[/bold] or [bold]chain-dig[/bold] first.")
            raise typer.Exit(code=1)
        return None
    from .taste import _migrate_schema
    conn = _sqlite3.connect(str(db_path))
    _migrate_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# chain-dig command
# ---------------------------------------------------------------------------

@app.command(name="chain-dig")
def chain_dig_cmd(
    seeds: list[str] = typer.Argument(..., help="URLs or 'Artist - Track' text"),
    hops: int = typer.Option(2, "--hops", help="Number of hops to chain (1-4)"),
    branches: int = typer.Option(3, "--branches", "-b", help="Branches to follow per hop"),
    limit: int = typer.Option(20, "--limit", "-l", help="Max results"),
    limit_per_seed: int = typer.Option(50, "--limit-per-seed", hidden=True),
    deep: bool = typer.Option(False, "--deep", help="Fetch 200 per seed for max depth"),
    trail_name: str | None = typer.Option(None, "--name", "-n", help="Trail name"),
    no_trail: bool = typer.Option(False, "--no-trail", help="Disable trail persistence"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show hop column"),
    hop_decay: float = typer.Option(0.85, "--hop-decay", hidden=True),
    max_discogs_calls: int = typer.Option(100, "--max-discogs-calls", hidden=True),
    youtube: bool = typer.Option(False, "--youtube", "-y", help="Create YouTube playlist"),
    playlist_name: str | None = typer.Option(None, "--playlist-name", "-p"),
    save: str | None = typer.Option(None, "--save", help="Save as cosine.club playlist"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Export to JSON"),
    taste_db: str | None = typer.Option(None, "--db", hidden=True, help="Taste DB path for gap detection"),
    scorer: str = typer.Option("rrf", "--scorer", callback=_validate_scorer, help=f"Scoring strategy: {', '.join(SCORERS)}"),
):
    """Multi-hop discovery: follow the thread through labels, artists, and producers."""
    from .chain import chain_dig
    from .session import (
        auto_session_name, create_session, record_results,
        record_branches, get_convergence, mark_branch_explored,
    )
    from .taste import TasteProfile
    from rich.tree import Tree
    import sqlite3

    if deep:
        limit_per_seed = 200
    hops = max(1, min(hops, 4))  # clamp to 1-4

    taste_profile = None
    db_conn = None

    # Track all discovered branches and which were followed
    all_discovered_branches: list[dict] = []
    followed_branch_values: set[tuple[int, str, str]] = set()

    try:
        with CosineClient() as client:
            # Resolve seeds
            resolved_seeds: list[Track] = []
            for seed in seeds:
                tracks = resolve_seed(seed, client)
                if not tracks:
                    console.print(f"[yellow]Seed not found: {seed} (skipped)[/]")
                    continue
                resolved_seeds.extend(tracks[:1])

            if not resolved_seeds:
                console.print("[red]No seeds could be resolved.[/]")
                raise typer.Exit(code=1)

            console.print(f"\n[bold]Seeds for chain-dig ({len(resolved_seeds)}):[/]")
            for i, s in enumerate(resolved_seeds, 1):
                console.print(f"  {i}. {s.artist} - {_track_title(s)} [dim]({s.id})[/dim]")

            # Taste profile (optional)
            taste_profile = None
            gap_artists_set: set[str] = set()
            if taste_db:
                try:
                    taste_profile = TasteProfile(taste_db)
                except Exception:
                    console.print("[yellow]Could not load taste DB -- gap detection disabled[/]")

            # Session/trail setup BEFORE chain-dig so on_progress can record branches
            session_id = None
            if not no_trail:
                db_path = Path(taste_db).expanduser() if taste_db else load_config().db_path
                try:
                    from .taste import _migrate_schema
                    db_path.parent.mkdir(parents=True, exist_ok=True)
                    db_conn = sqlite3.connect(str(db_path))
                    _migrate_schema(db_conn)
                    name = trail_name or auto_session_name(list(seeds))
                    session_id = create_session(db_conn, name, list(seeds))
                except sqlite3.OperationalError as e:
                    console.print(f"[yellow]Could not create trail: {e}[/]")

            # Progress tree
            progress_tree = Tree("[bold]Chain-dig[/]")

            def on_progress(event: str, data: dict):
                if event == "hop_start":
                    hop = data.get("hop", 0)
                    if hop == 0:
                        progress_tree.add(f"Hop 0: Fusing {data.get('seed_count', '?')} seeds...")
                    else:
                        progress_tree.add(f"Hop {hop}: Extracting branches...")
                elif event == "hop_done":
                    count = data.get("result_count", 0)
                    progress_tree.add(f"  [green]Found {count} tracks[/green]")
                elif event == "branch_start":
                    btype = data.get("branch_type", "")
                    bval = data.get("branch", "?")
                    progress_tree.add(f"  Following: {bval} ({btype})")
                elif event == "branch_done":
                    count = data.get("result_count", 0)
                    bval = data.get("branch", "?")
                    followed_branch_values.add((data.get("hop", 0), data.get("branch_type", ""), bval))
                    progress_tree.add(f"    [green]{bval} -> {count} tracks[/green]")
                elif event in ("hop_error", "branch_error"):
                    progress_tree.add(f"  [yellow]Discovery step failed: {data.get('error', 'unknown error')}[/yellow]")
                elif event == "branches_discovered":
                    # Record ALL branches to the trail DB
                    hop_num = data.get("hop", 1)
                    branch_objs = data.get("branches", [])
                    if session_id and db_conn and branch_objs:
                        try:
                            branch_dicts = [
                                {
                                    "branch_type": b.type,
                                    "branch_value": b.value,
                                    "branch_id_ext": b.discogs_id,
                                    "branch_score": b.score,
                                }
                                for b in branch_objs
                            ]
                            branch_ids = record_branches(db_conn, session_id, branch_dicts, hop_num)
                            # Store mapping for marking explored later
                            all_discovered_branches.append({
                                "hop": hop_num,
                                "branches": branch_objs,
                                "branch_ids": branch_ids,
                            })
                        except Exception:
                            pass  # DB failures don't crash the dig
                elif event == "budget_exhausted":
                    progress_tree.add("  [yellow]Discogs budget exhausted -- merging what we have[/yellow]")
                elif event == "merging":
                    progress_tree.add(f"Merging results across {data.get('hop_count', '?')} hops...")
                elif event == "done":
                    progress_tree.add(f"[bold green]Done: {data.get('total_results', 0)} results[/bold green]")
                console.print(progress_tree)

            ranked = chain_dig(
                seeds=[s.id for s in resolved_seeds],
                cosine_client=client,
                hops=hops,
                branches=branches,
                limit_per_seed=limit_per_seed,
                taste_profile=taste_profile,
                on_progress=on_progress,
                hop_decay=hop_decay,
                max_discogs_calls=max_discogs_calls,
                scorer=scorer,
                seed_artists={seed.artist for seed in resolved_seeds if seed.artist},
            )
            ranked = ranked[:limit]

            if not ranked:
                console.print("[yellow]No results found.[/]")
                return

            # Gap detection
            if taste_profile:
                result_artists = {item.track.artist for item in ranked}
                gap_artists_set = taste_profile.gap_artists(result_artists)

            # Record results
            if session_id and db_conn:
                try:
                    # Record each result with its actual hop number
                    for rt in ranked:
                        record_results(db_conn, session_id, [rt], hop=getattr(rt, "hop", 0))
                except Exception:
                    pass

            # Mark followed branches as explored
            if session_id and db_conn:
                for entry in all_discovered_branches:
                    for branch_obj, branch_db_id in zip(entry["branches"], entry["branch_ids"]):
                        if (entry["hop"], branch_obj.type, branch_obj.value) in followed_branch_values:
                            try:
                                mark_branch_explored(db_conn, branch_db_id)
                            except Exception:
                                pass

            # Convergence
            convergence: dict[str, int] = {}
            if db_conn:
                try:
                    track_ids = [item.track.id for item in ranked]
                    convergence = get_convergence(db_conn, track_ids)
                except Exception:
                    pass

            # Display results
            console.print(_render_chain_table(ranked, verbose=verbose, gap_artists=gap_artists_set, convergence=convergence))

            # Summary line
            new_count = len(gap_artists_set & {item.track.artist for item in ranked}) if gap_artists_set else 0
            conv_count = sum(1 for v in convergence.values() if v >= 2)
            parts = []
            if new_count:
                parts.append(f"[green]{new_count} results from artists not in your collection[/green]")
            if conv_count:
                parts.append(f"[cyan]{conv_count} tracks found via 2+ paths[/cyan]")
            if parts:
                console.print(" ".join(parts))

            # YouTube/cosine playlist/JSON export
            if youtube or save or output:
                _finish_dig(
                    client=client,
                    ranked=ranked,
                    mode="centroid",
                    seeds=list(seeds),
                    resolved_seeds=resolved_seeds,
                    limit=limit,
                    youtube=youtube,
                    playlist_name=playlist_name,
                    save=save,
                    output=output,
                    skip_table=True,
                )

            # Detect leads across digs
            if db_conn:
                try:
                    from .leads import detect_leads
                    new_leads = detect_leads(db_conn)
                    if new_leads:
                        top = new_leads[0]
                        console.print(
                            f"\n[dim]Lead: {top['value']} ({top['type']}) "
                            f"-- appeared in {top['appearances']} digs[/dim]"
                        )
                except Exception:
                    pass  # Lead detection failures don't crash the dig

    except CosineClientError as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(code=1)
    finally:
        if taste_profile:
            taste_profile.close()
        if db_conn:
            db_conn.close()


# ---------------------------------------------------------------------------
# taste scan / taste-dig commands
# ---------------------------------------------------------------------------


@app.command(name="taste-scan")
def taste_scan_cmd(
    music_dir: str = typer.Argument(..., help="Path to your music directory"),
    db: str = typer.Option(None, "--db", help="Taste DB output path"),
    tags: bool = typer.Option(True, "--tags/--no-tags", help="Read ID3/Vorbis tags (requires mutagen)"),
    full: bool = typer.Option(False, "--full", help="Full rescan (default is incremental)"),
):
    """Scan a local music directory and build/update the taste DB.

    Incremental by default -- only processes new files not already in the DB.
    Use --full to rescan everything.

    The music directory is treated as read-only -- nothing is written to it.
    """
    music_path = Path(music_dir).expanduser().resolve()
    if not music_path.is_dir():
        console.print(f"[red]Not a directory:[/] {music_path}")
        raise typer.Exit(code=1)

    db_path = Path(db).expanduser() if db else load_config().db_path
    taste_dir = db_path.parent
    taste_dir.mkdir(parents=True, exist_ok=True)

    if full:
        from .taste import export_manifest, build_taste_db

        manifest_path = taste_dir / "manifest.jsonl"
        console.print(f"Full scan of [bold]{music_path}[/bold] ...")
        count = export_manifest(music_path, manifest_path, read_tags=tags)
        if count == 0:
            console.print("[yellow]No audio files found.[/]")
            raise typer.Exit(code=1)
        conn = build_taste_db(manifest_path, db_path)
        artists = conn.execute("SELECT COUNT(DISTINCT artist) FROM tracks WHERE artist IS NOT NULL").fetchone()[0]
        labels = conn.execute("SELECT COUNT(DISTINCT label) FROM tracks WHERE label IS NOT NULL").fetchone()[0]
        conn.close()
        console.print(f"[green]Done.[/] {count} tracks, {artists} artists, {labels} labels.")
    else:
        from .taste import incremental_scan

        console.print(f"Scanning [bold]{music_path}[/bold] for new files ...")
        new_count, total = incremental_scan(music_path, db_path, read_tags=tags)
        if new_count == 0:
            console.print(f"No new files. DB has {total} tracks.")
        else:
            console.print(f"[green]Added {new_count} new files.[/] DB now has {total} tracks.")

    console.print(f"Run [bold]cosine-digger taste-dig[/bold] to discover music from your library.")


@app.command(name="taste-dig")
def taste_dig_cmd(
    hops: int = typer.Option(2, "--hops", help="Number of hops to chain"),
    branches: int = typer.Option(3, "--branches", "-b"),
    limit: int = typer.Option(20, "--limit", "-l"),
    window: str = typer.Option("365d", "--window", "-w", help="Time window: 90d, 365d, all"),
    db: str = typer.Option(None, "--db", help="Taste DB path"),
    trail_name: str | None = typer.Option(None, "--name", "-n", help="Trail name"),
    no_trail: bool = typer.Option(False, "--no-trail"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    deep: bool = typer.Option(False, "--deep"),
    youtube: bool = typer.Option(False, "--youtube", "-y"),
    save: str | None = typer.Option(None, "--save"),
    scorer: str = typer.Option("rrf", "--scorer", callback=_validate_scorer, help=f"Scoring strategy: {', '.join(SCORERS)}"),
):
    """Zero-input discovery from your music library. Uses taste DB to generate seeds, then chain-digs."""
    from .taste import TasteProfile

    db_path = db or str(load_config().db_path)
    if not Path(db_path).exists():
        console.print("[red]No taste DB found.[/red] Scan your library with `taste-scan` first.")
        raise typer.Exit(code=1)

    try:
        taste = TasteProfile(db_path)
        seeds = taste.generate_dig_seeds(window=window, max_seeds=6, verified_only=True)
        taste.close()
    except Exception as exc:
        console.print(f"[red]Taste DB error:[/] {exc}")
        raise typer.Exit(code=1)

    if not seeds:
        console.print(f"[yellow]No seeds found in taste DB for window '{window}'.[/]")
        console.print("Try --window all or run taste verification first.")
        raise typer.Exit(code=1)

    console.print(f"[bold]Taste-dig: {len(seeds)} seeds from your library ({window})[/]")

    # Delegate to chain-dig
    chain_dig_cmd(
        seeds=seeds,
        hops=hops,
        branches=branches,
        limit=limit,
        limit_per_seed=200 if deep else 50,
        deep=deep,
        trail_name=trail_name,
        no_trail=no_trail,
        verbose=verbose,
        hop_decay=0.85,
        max_discogs_calls=100,
        youtube=youtube,
        playlist_name=None,
        save=save,
        output=None,
        taste_db=db_path,
        scorer=scorer,
    )


# ---------------------------------------------------------------------------
# Lead discovery commands
# ---------------------------------------------------------------------------


@app.command(name="next")
def next_cmd():
    """Review your discoveries -- save, pass, or dig deeper."""
    import webbrowser
    from urllib.parse import quote as url_quote
    from .leads import detect_leads, get_lead_tracks, mute_lead, record_event, get_unactioned_results
    from .session import list_sessions

    conn = _open_db()
    try:
        sessions = list_sessions(conn)
        if not sessions:
            console.print("No discoveries yet. Run a chain-dig first:")
            console.print("  cosine-digger chain-dig [bold]\"Artist - Track\"[/bold]")
            console.print("  cosine-digger taste-dig")
            return

        # Detect leads
        leads = detect_leads(conn)
        tracks: list[dict] = []
        lead_header = ""
        lead_idx = -1  # -1 = no active lead (fallback mode)

        if leads:
            for i, lead_candidate in enumerate(leads):
                tracks = get_lead_tracks(conn, lead_candidate["type"], lead_candidate["value"], limit=200)
                if tracks:
                    lead_idx = i
                    lead_header = (
                        f"[bold]LEAD: {lead_candidate['value']} ({lead_candidate['type']})[/bold] "
                        f"-- appeared in {lead_candidate['appearances']} digs"
                    )
                    break

        if not tracks:
            tracks = get_unactioned_results(conn, limit=200)
            if not tracks:
                console.print("[dim]All caught up! No unreviewed tracks. Run another chain-dig.[/dim]")
                return
            lead_header = "[dim]Your recent discoveries (no leads yet -- run more digs)[/dim]"

        console.print(f"\n{lead_header}\n")

        # Show tracks table
        table = Table(show_lines=True, border_style="dim")
        table.add_column("#", justify="right", width=3)
        table.add_column("Artist - Track", min_width=25)
        table.add_column("Score", justify="right", width=6)
        table.add_column("Found via", max_width=25)

        for i, t in enumerate(tracks, 1):
            artist_track = f"{t['artist']} - {t['track_name']}"
            score = f"{t['score']:.2f}" if t.get("score") else ""
            table.add_row(str(i), artist_track, score, t.get("via", ""))

        console.print(table)

        # Interactive loop (TTY only)
        is_tty = sys.stdout.isatty() and sys.stdin.isatty()
        if not is_tty:
            return

        track_idx = 0
        while True:
            # Auto-advance to next lead when current tracks exhausted
            while track_idx >= len(tracks):
                if lead_idx < 0:
                    # Fallback mode (no leads) — nothing more to show
                    console.print("[dim]All caught up![/dim]")
                    return
                found_next = False
                while lead_idx + 1 < len(leads):
                    lead_idx += 1
                    next_lead = leads[lead_idx]
                    tracks = get_lead_tracks(conn, next_lead["type"], next_lead["value"], limit=200)
                    if tracks:
                        console.print(
                            f"\n[bold]LEAD: {next_lead['value']} ({next_lead['type']})[/bold] "
                            f"-- appeared in {next_lead['appearances']} digs\n"
                        )
                        track_idx = 0
                        found_next = True
                        break
                if not found_next:
                    console.print("[dim]No more leads. All caught up![/dim]")
                    return

            t = tracks[track_idx]
            console.print(
                f"\n[bold]Track {track_idx + 1}/{len(tracks)}:[/] "
                f"{t['artist']} - {t['track_name']}"
            )
            try:
                action = input("  [s]ave [.]pass [d]ig [p]lay [m]ute [n]ext [q]uit: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Done.[/dim]")
                return

            if not action:
                track_idx += 1
                continue

            cmd = action[0]

            if cmd == "s":
                record_event(conn, t["id"], "save")
                console.print("[green]Saved.[/green]")
                track_idx += 1
            elif cmd == ".":
                record_event(conn, t["id"], "pass")
                track_idx += 1
            elif cmd == "d":
                record_event(conn, t["id"], "dig_from")
                console.print(f"[dim]Launching chain-dig from {t['artist']}...[/dim]")
                # Dig-from: launch inline chain-dig
                try:
                    seed = f"https://cosine.club/track/{t['track_id']}"
                    chain_dig_cmd(
                        seeds=[seed], hops=2, branches=3, limit=20,
                        limit_per_seed=50, deep=False, trail_name=None,
                        no_trail=False, verbose=False, hop_decay=0.85,
                        max_discogs_calls=100, youtube=False,
                        playlist_name=None, save=None, output=None,
                        taste_db=None, scorer="rrf",
                    )
                except (SystemExit, Exception):
                    pass
                track_idx += 1
            elif cmd == "p":
                yt_query = url_quote(f"{t['artist']} {t['track_name']}")
                yt_url = f"https://www.youtube.com/results?search_query={yt_query}"
                if is_tty:
                    try:
                        webbrowser.open(yt_url)
                    except Exception:
                        console.print(f"[dim]{yt_url}[/dim]")
                else:
                    console.print(f"[dim]{yt_url}[/dim]")
                # Don't advance -- let user listen then act
            elif cmd in ("m", "n"):
                if lead_idx < 0:
                    console.print("[dim]No leads to navigate.[/dim]")
                    continue
                if cmd == "m" and lead_idx < len(leads):
                    current_lead = leads[lead_idx]
                    mute_lead(conn, current_lead["type"], current_lead["value"])
                    console.print(f"[dim]Muted {current_lead['value']}.[/dim]")
                found_next = False
                while lead_idx + 1 < len(leads):
                    lead_idx += 1
                    next_lead = leads[lead_idx]
                    tracks = get_lead_tracks(conn, next_lead["type"], next_lead["value"], limit=200)
                    if tracks:
                        console.print(
                            f"\n[bold]LEAD: {next_lead['value']} ({next_lead['type']})[/bold] "
                            f"-- appeared in {next_lead['appearances']} digs\n"
                        )
                        track_idx = 0
                        found_next = True
                        break
                if not found_next:
                    console.print("[dim]No more leads.[/dim]")
                    return
            elif cmd == "q":
                console.print("[dim]Done.[/dim]")
                return
    finally:
        conn.close()


@app.command(name="leads")
def leads_cmd(
    unmute: bool = typer.Option(False, "--unmute", help="List and unmute muted leads"),
):
    """Show patterns detected across your digs."""
    from .leads import detect_leads, get_muted_leads, mute_lead, unmute_lead

    conn = _open_db()
    try:
        if unmute:
            muted = get_muted_leads(conn)
            if not muted:
                console.print("No muted leads.")
                return
            console.print("[bold]Muted leads:[/bold]")
            for i, m in enumerate(muted, 1):
                console.print(f"  {i}. {m['value']} ({m['type']}) -- muted {m['muted_at'][:10]}")

            is_tty = sys.stdout.isatty() and sys.stdin.isatty()
            if not is_tty:
                return

            while True:
                try:
                    raw = input("\nUnmute which? Enter row number or [q]uit: ").strip()
                except (KeyboardInterrupt, EOFError):
                    return
                if not raw or raw.lower().startswith("q"):
                    return
                try:
                    idx = int(raw) - 1
                    if not (0 <= idx < len(muted)):
                        console.print(f"[yellow]Enter 1-{len(muted)} or q.[/yellow]")
                        continue
                except ValueError:
                    console.print(f"[yellow]Enter 1-{len(muted)} or q.[/yellow]")
                    continue
                target = muted[idx]
                try:
                    confirm = input(f"  Unmute \"{target['type']}: {target['value']}\"? [y/n]: ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    return
                if confirm == "y":
                    unmute_lead(conn, target["type"], target["value"])
                    console.print(f"[green]Unmuted {target['value']}.[/green]")
                return

        leads = detect_leads(conn)
        if not leads:
            console.print("No leads yet. Run 2+ chain-digs to start detecting patterns.")
            return

        table = Table(title="Your Leads", show_lines=True, border_style="dim")
        table.add_column("#", justify="right", width=3)
        table.add_column("Lead", min_width=20)
        table.add_column("Type", width=10)
        table.add_column("Appeared", justify="right", width=10)
        table.add_column("Tracks", justify="right", width=8)
        table.add_column("Last seen", max_width=20)

        for i, lead in enumerate(leads, 1):
            table.add_row(
                str(i),
                lead["value"],
                lead["type"],
                f"{lead['appearances']} digs",
                str(lead.get("total_tracks", 0)),
                (lead.get("last_seen") or "")[:16],
            )

        console.print(table)
        console.print(f"[dim]{len(leads)} active leads[/dim]")

        is_tty = sys.stdout.isatty() and sys.stdin.isatty()
        if not is_tty:
            return

        while True:
            try:
                raw = input("\nMute a lead? Enter row number or [q]uit: ").strip()
            except (KeyboardInterrupt, EOFError):
                return
            if not raw or raw.lower().startswith("q"):
                return
            try:
                idx = int(raw) - 1
                if not (0 <= idx < len(leads)):
                    console.print(f"[yellow]Enter 1-{len(leads)} or q.[/yellow]")
                    continue
            except ValueError:
                console.print(f"[yellow]Enter 1-{len(leads)} or q.[/yellow]")
                continue
            target = leads[idx]
            try:
                confirm = input(f"  Mute \"{target['type']}: {target['value']}\"? [y/n]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                return
            if confirm == "y":
                mute_lead(conn, target["type"], target["value"])
                console.print(f"[dim]Muted {target['value']}.[/dim]")
            return
    finally:
        conn.close()


@app.command(name="recap")
def recap_cmd(
    days: int = typer.Option(7, "--days", "-d", help="Number of days to look back"),
):
    """Summary of recent discovery activity."""
    from .leads import get_recap

    conn = _open_db()
    try:
        stats = get_recap(conn, days=days)

        if stats["dig_count"] == 0:
            console.print(f"No digs in the last {days} days.")
            return

        console.print(f"\n[bold]Last {days} days:[/bold] {stats['dig_count']} digs, "
                       f"{stats['tracks_found']} tracks found, "
                       f"{stats['saves']} saved, {stats['passes']} passed")

        if stats["hot_leads"]:
            console.print("\n[bold]Hot leads:[/bold]")
            for lead in stats["hot_leads"][:5]:
                console.print(
                    f"  {lead['value']} ({lead['type']}) "
                    f"-- appeared {lead['appearances']}x"
                )

        if stats["convergence_count"]:
            console.print(f"\n[cyan]{stats['convergence_count']} tracks found via 2+ independent paths[/cyan]")

        console.print(f"[dim]{stats['new_artist_count']} unique artists discovered[/dim]")
    finally:
        conn.close()


@app.command(name="saved")
def saved_cmd(
    dig: bool = typer.Option(False, "--dig", help="Chain-dig from your saved tracks"),
    youtube: bool = typer.Option(False, "--youtube", "-y", help="Create YouTube playlist from saves"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max tracks to show/use"),
    clear: bool = typer.Option(False, "--clear", help="Clear all saved tracks"),
    yes: bool = typer.Option(False, "--yes", help="Allow clearing saved tracks without a TTY"),
):
    """View and act on tracks you saved from discovery sessions."""
    from .leads import get_saved_tracks

    conn = _open_db()
    try:
        if clear:
            count = conn.execute(
                "SELECT COUNT(*) FROM result_events WHERE event_type IN ('save', 'dig_from')"
            ).fetchone()[0]
            if count == 0:
                console.print("No saved tracks to clear.")
                return
            is_tty = sys.stdout.isatty() and sys.stdin.isatty()
            if not is_tty and not yes:
                console.print(f"Would clear {count} saved tracks. Use --yes to confirm without a TTY.")
                raise typer.Exit(code=1)
            if is_tty:
                try:
                    confirm = input(f"Clear {count} saved tracks? [y/n]: ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    return
                if confirm != "y":
                    return
            conn.execute("DELETE FROM result_events WHERE event_type IN ('save', 'dig_from')")
            conn.commit()
            console.print(f"[dim]Cleared {count} saved tracks.[/dim]")
            return

        tracks = get_saved_tracks(conn, limit=limit)
        if not tracks:
            console.print("No saved tracks yet. Use [bold][s]ave[/bold] in the [bold]next[/bold] command to save discoveries.")
            return

        if dig:
            # Show table so user can see what they're picking from
            table = Table(title="Saved Tracks", show_lines=True, border_style="dim")
            table.add_column("#", justify="right", width=3)
            table.add_column("Artist - Track", min_width=25)
            table.add_column("Score", justify="right", width=6)
            table.add_column("Found via", max_width=25)

            for i, t in enumerate(tracks, 1):
                artist_track = f"{t['artist']} - {t['track_name']}"
                if t["event_type"] == "dig_from":
                    artist_track += " [dim][dug][/dim]"
                score = f"{t['score']:.2f}" if t.get("score") else ""
                table.add_row(str(i), artist_track, score, t.get("via", ""))

            console.print(table)

            # Let user pick which tracks to dig from
            is_tty = sys.stdout.isatty() and sys.stdin.isatty()
            if is_tty:
                try:
                    raw = input(f"\nDig from which? Enter numbers (e.g. 1,3,5) or [a]ll: ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    return
                if not raw or raw.startswith("q"):
                    return
                if raw.startswith("a"):
                    selected = tracks[:6]
                else:
                    indices = []
                    for part in raw.replace(" ", ",").split(","):
                        part = part.strip()
                        if part.isdigit():
                            idx = int(part) - 1
                            if 0 <= idx < len(tracks):
                                indices.append(idx)
                    if not indices:
                        console.print("[yellow]No valid track numbers. Try again.[/yellow]")
                        return
                    selected = [tracks[i] for i in indices]
            else:
                selected = tracks[:6]

            seeds = [f"https://cosine.club/track/{t['track_id']}" for t in selected]
            console.print(f"\n[bold]Digging from {len(seeds)} tracks...[/bold]\n")
            conn.close()
            chain_dig_cmd(
                seeds=seeds, hops=2, branches=3, limit=20,
                limit_per_seed=50, deep=False, trail_name=None,
                no_trail=False, verbose=False, hop_decay=0.85,
                max_discogs_calls=100, youtube=False,
                playlist_name=None, save=None, output=None,
                taste_db=None, scorer="rrf",
            )
            return

        if youtube:
            console.print(f"[bold]Creating YouTube playlist from {len(tracks)} saved tracks...[/bold]")
            missing = [track for track in tracks if not track.get("video_id")]
            if missing:
                try:
                    with CosineClient() as cosine:
                        for track in missing:
                            try:
                                source = cosine.similar(track["track_id"], limit=1).source_track
                            except CosineClientError as exc:
                                console.print(f"[yellow]Could not resolve saved track {track['track_id']}: {exc}[/]")
                                continue
                            if source.id == track["track_id"] and source.video_id:
                                track["video_id"] = source.video_id
                                conn.execute(
                                    "UPDATE session_results SET video_id = ? WHERE track_id = ? AND video_id IS NULL",
                                    (source.video_id, source.id),
                                )
                    conn.commit()
                except CosineClientError as exc:
                    console.print(f"[yellow]Could not resolve older saves: {exc}[/]")
            video_ids = [track["video_id"] for track in tracks if track.get("video_id")]
            unresolved = len(tracks) - len(video_ids)
            if unresolved:
                console.print(f"[yellow]{unresolved} saved track(s) have no verified YouTube video and will be omitted.[/]")
            if not video_ids:
                console.print("[yellow]No verified YouTube videos available for saved tracks.[/]")
                raise typer.Exit(code=1)
            saved = [Track(id=track["track_id"], name=track["track_name"] or "", artist=track["artist"] or "",
                           track=track["track_name"] or "", video_id=track.get("video_id")) for track in tracks]
            report = _youtube_export(saved, "Saved Discoveries", "Saved tracks from cosine-digger", conn=conn)
            if not (report and report.verified):
                raise typer.Exit(code=1)
            return

        # Default: show saved tracks table
        table = Table(title="Saved Tracks", show_lines=True, border_style="dim")
        table.add_column("#", justify="right", width=3)
        table.add_column("Artist - Track", min_width=25)
        table.add_column("Score", justify="right", width=6)
        table.add_column("Found via", max_width=25)
        table.add_column("Saved", max_width=12)

        for i, t in enumerate(tracks, 1):
            artist_track = f"{t['artist']} - {t['track_name']}"
            if t["event_type"] == "dig_from":
                artist_track += " [dim][dug][/dim]"
            score = f"{t['score']:.2f}" if t.get("score") else ""
            saved_at = (t.get("saved_at") or "")[:10]
            table.add_row(str(i), artist_track, score, t.get("via", ""), saved_at)

        console.print(table)
        console.print(f"\n[dim]{len(tracks)} saved tracks. Try:[/dim]")
        console.print("[dim]  cosine-digger saved --dig       discover more music like these[/dim]")
        console.print("[dim]  cosine-digger saved --youtube   make a YouTube playlist[/dim]")
    finally:
        try:
            conn.close()
        except Exception:
            pass


@app.command(name="context")
def context_cmd(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON for agent consumption"),
):
    """Show your full discovery state — leads, saves, taste, sessions."""
    import json as json_mod
    from .leads import get_discovery_context

    conn = _open_db(require=False)
    if conn is None:
        if json_output:
            print(json_mod.dumps({"session_count": 0, "total_discoveries": 0, "leads": [], "saves": [], "muted": [], "taste_seeds": [], "taste_stats": None}))
        else:
            console.print("[dim]No discovery data yet. Run a chain-dig to get started.[/dim]")
        return

    try:
        taste_profile = None
        taste_db_path = load_config().db_path
        if taste_db_path.exists():
            from .taste import TasteProfile
            try:
                taste_profile = TasteProfile(str(taste_db_path))
            except Exception:
                pass

        ctx = get_discovery_context(conn, taste_profile=taste_profile)

        if taste_profile:
            taste_profile.close()

        if json_output:
            print(json_mod.dumps(ctx, default=str))
            return

        console.print("\n[bold]Discovery State[/bold]")
        console.print("=" * 40)
        console.print(f"Sessions: {ctx['session_count']} chain-digs")
        console.print(f"Discoveries: {ctx['total_discoveries']} tracks found")

        save_count = len([s for s in ctx["saves"] if s["event_type"] == "save"])
        dig_count = len([s for s in ctx["saves"] if s["event_type"] == "dig_from"])
        if save_count or dig_count:
            parts = []
            if save_count:
                parts.append(f"{save_count} saved")
            if dig_count:
                parts.append(f"{dig_count} dug-from")
            console.print(f"Saves: {', '.join(parts)}")

        lead_count = len(ctx["leads"])
        muted_count = len(ctx["muted"])
        if lead_count:
            console.print(f"Leads: {lead_count} active" + (f" ({muted_count} muted)" if muted_count else ""))

        if ctx["leads"]:
            console.print("\n[bold]Top leads:[/bold]")
            for i, lead in enumerate(ctx["leads"][:5], 1):
                console.print(
                    f"  {i}. {lead['value']} ({lead['type']}) "
                    f"-- {lead['appearances']} digs, {lead.get('total_tracks', 0)} tracks"
                )

        if ctx["saves"]:
            console.print("\n[bold]Recent saves:[/bold]")
            for i, s in enumerate(ctx["saves"][:5], 1):
                badge = " [dim][dug][/dim]" if s["event_type"] == "dig_from" else ""
                via = f" [dim](via {s['via']})[/dim]" if s.get("via") and s["via"] != "seed" else ""
                console.print(f"  {i}. {s['artist']} - {s['track_name']}{badge}{via}")

        if ctx["taste_stats"]:
            ts = ctx["taste_stats"]
            console.print(
                f"\n[bold]Taste DB:[/bold] {ts['track_count']} tracks, "
                f"{ts['artist_count']} artists, {ts['label_count']} labels"
            )
            if ctx["taste_seeds"]:
                console.print("[dim]  Seeds: " + ", ".join(ctx["taste_seeds"][:3]) + "[/dim]")

        console.print()
    finally:
        conn.close()


from .commands import register_all as _register_commands  # noqa: E402

_register_commands(app)


if __name__ == "__main__":
    app()
