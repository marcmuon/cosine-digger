# cosine-digger CLI Design System

## Color Semantics

| Color | Usage | Example |
|-------|-------|---------|
| (none) | Primary content — artist names, track titles | `Pearson Sound - Clutch` |
| `[dim]` | Metadata, secondary info — IDs, timestamps, borders | `(track_id)`, table borders |
| `[green]` | Success, new discoveries | `[NEW]` badge, "Playlist saved: <URL>" |
| `[cyan]` | Convergence signals, cross-session | `[x2]` badge, session references |
| `[yellow]` | Warnings, partial results, degraded states | "3/5 seeds resolved", "Budget exhausted" |
| `[red]` | Errors only | "cosine.club unavailable" |
| `[bold]` | Section headers, trail names, seed list | "Seeds for fusion (6):", "Trail: theo-parrish-2026-03-22" |

## Table Conventions

- `show_lines=True` for dig result tables
- `border_style="dim"`
- Right-align numeric columns (Score, Rank)
- Left-align text columns (Artist-Track, Via)
- Column priority (drop right-to-left when terminal narrows):
  1. Rank + Artist-Track (always shown)
  2. Via (always shown — this is the story)
  3. Score (drop below 80 cols)
  4. Links (drop below 120 cols)

## Terminal Width Behavior

| Width | Columns shown |
|-------|--------------|
| 120+ cols | Rank, Artist-Track, Via, Score, Links |
| 80-119 cols | Rank, Artist-Track, Via, Score |
| <80 cols | Rank, Artist-Track, Via |

## Badges

Inline with Artist-Track column, styled to not overpower the name:
- `[green][NEW][/green]` — artist not in user's collection (taste-dig)
- `[cyan][x2][/cyan]` — track found via 2+ independent discovery paths
- Badges appear after the track title, space-separated

## Progress Display

- **Multi-step operations** (chain-dig): Rich Tree, animated branch-by-branch
- **Single-step waits** (API calls): Rich Spinner
- **Completed steps**: annotated with duration ("12s", "done")
- **Failed steps**: `[yellow]` with explanation, tree continues

## Lead Detection Display

After chain-dig results, a one-liner surfaces if cross-dig patterns are detected:
- `[dim]Lead: Hessle Audio (label) -- appeared in 3 digs[/dim]`

The `next` command shows a listening mission:
- Lead header: `[bold]LEAD: Hessle Audio (label)[/bold] -- appeared in 4 digs`
- Track table with #, Artist - Track, Score, Found via
- Action prompt (two lines):
  `[s]ave  [.]pass  [d]ig  [p]lay`
  `[m]ute  [n]ext  [q]uit`

The `leads` command shows a summary table of all detected patterns.

## Interactive Feedback

Track-level feedback via the `next` command:
- `save` — marks a track as interesting
- `pass` — not interested right now, don't resurface this track (does not affect lead ranking)
- `dig_from` — launches an inline chain-dig from this track
- Enter (empty) — flip to next track without recording anything
- Events stored in `result_events` table with latest-event-wins (UNIQUE on result_id)

Lead-level control via the `leads` command:
- Mute a lead to stop it from appearing in `next` (explicit, permanent until unmuted)
- Unmute via `leads --unmute`

## Empty States

Every empty state has: warm message + what to do next. Never just "No items."
- "No results" → "No seeds resolved. Check your input and try again."
- "No discoveries" → "No discoveries yet. Run a chain-dig first."
- "All caught up" → "All caught up! No unreviewed tracks. Run another chain-dig."
- "No leads" → "No leads yet. Run 2+ chain-digs to start detecting patterns."
- "No taste DB" → "No taste DB found. Scan your library with `taste scan` first."

## Error Messages

Format: what happened + why + what to do.
- Bad: "Error: 429"
- Good: "Discogs rate limit hit — showing results found so far. Try again in 60s for deeper results."

## Accessibility

- `NO_COLOR` env var respected (Rich built-in)
- `--json` for programmatic output
- `--no-color` flag alias
- All interactive elements keyboard-navigable (questionary built-in)
