# Slice product/UX spec: milestone 1

Stage 1 of 4 (Product/UX → approved interaction spec → implementation → UX verification). Written 1 October 2026. This file turns the approved vertical-slice design into the spec the build follows: every region, state, string and interaction of milestone 1. Its checklist twin is `docs/slice/ux-acceptance.md`.

**Binding sources, in order.** If this file disagrees with them, they win; report the disagreement.

1. `docs/implementation-handoff.md`: decisions 1–13, token aliases, components, pages, responsive rules, must-not-change, edge cases, acceptance criteria 1–19.
2. `docs/vertical-slice.html` (source `.planning/2026-10-01-vertical-slice/report/slice.src.html`): revisions R1–R19 (§03), behaviour (§07), keys (§08), states (§09), graph changes (§10), and the ASCII frames in `.planning/2026-10-01-vertical-slice/wire/*.txt`.
3. `docs/dig-architectures.html` §02: the shared model and the rules every design keeps.

**Nothing here reopens an approved decision.** Where the sources leave something open, §10 records the choice (D1–D44). Where two approved sources pull against each other, §11 records the default the build uses until the owner rules (C1–C2).

**Out of scope, and not specified here:** crate-to-playlist export, Daily and Weekly as openable digs, "Already know", phone or touch layouts, and any LLM.

**Notation.**
- `S-…` ids point into the string catalog in §8. Quoted text is exact wording. `{name}` is filled in at runtime.
- Tokens are the existing `:root` properties in `src/cosine_digger/web/static/app.css` plus the handoff aliases (`--st-*`, `--mark-*`, `--node-*`). This spec adds no hex values.
- Glyphs are Unicode, set in `--f-body` wherever a pixel font lacks them: ▶ playing, ❚❚ paused, ★ kept, ✕ passed, ~ running, ! warning or error, ‖ stopped, + new, ■ label, ◯ person, ★ origin (on maps), » one step further, ◆ track.
- "Pick" means a track a step returned. "Hub" means a label or credited person that 2 or more picks in a step share, as a Discogs fact. "Origin" means the point or points a step started from.

---

## 0. Completeness check

The repo's product-UX order was run over the slice as a completeness check, not a redesign: jobs, objects and state, screens and regions, element states, strings, interactions, keys and focus, map grammar, edge cases, acceptance.

**Method gap.** `.claude/rules/product-ux.md` and the `music-digging-ux` skill exist only in the main checkout (untracked), and reading them from this lane needed a permission that was never granted. The check therefore uses the music-digging-ux musts and the seven orientation questions as recorded in `.planning/2026-10-01-ux-diagnosis/findings.md` ("Dig walk (music-digging-ux)") and `docs/dig-architectures.html` §06. Re-run this section against the rule file if it differs.

### 0.1 Musts (★ = must)

| music-digging-ux principle | Where this spec meets it |
|---|---|
| The dig is the core object | §3.1 opens on your last dig or the seed box; the trail, dig switcher and kept list (§3.2); digs saved and reopened (§5.5) |
| ★ Persistent playback | The faceplate is the player and never unmounts (§2.3, §5.2) |
| ★ Connection semantics, no unexplained picks | Header sentence (§3.3), why lines, "Why it's here" kept apart from "Facts" (§3.5), hubs only from Discogs facts (§7), never "released on" (§8.3) |
| ★ Cheap branching | One key per move (T L A C, M); several moves at once; a finishing step never takes the view (§5.4, §5.7) |
| History is valuable | Trail tree, snapshots, return by trail, U, back or reload (§5.5); re-run makes a sibling (§5.6) |
| Audio is the primary evidence | Space, P, seek −15/+15/+60, heard marks (§5.2) |
| Progressive disclosure | Row buttons only on hover or selection; a key strip with the keys that matter for the focused region; facts only in the inspector (§2.5, §3.3) |
| ★ Spatial meaning | Radius from score, one sector per seed, hubs are facts, deterministic layout that never moves (§7) |
| Digging, not engagement | A step's queue ends with "where next"; a new step never autoplays (§5.2) |
| Expert power without overwhelming | Full key map, the ? sheet, 4 hints at a time (§6) |
| Orientation | The seven questions, answered below |
| One workspace | Player, trail, step, map and inspector on one screen (§2) |

### 0.2 The seven orientation questions, at the busiest moment

The busiest moment is frame F2: step 2 viewed, a track from step 2 playing, another pick selected, step 3 running.

| Question | Answer on screen |
|---|---|
| What is playing? | LCD line 1 (track, time), line 2 ("from step 2: …"), ▶ in the row's rank cell, ▶ on the trail step when you view another, ring on its dot |
| What is selected? | Row with the left bar and dashed outline; dashed square on its dot; the inspector shows it |
| Current seed or context? | Step header sentence and its "from step N" line; the viewing bar in the trail |
| Which filters are active? | None in the core; the header says when a move went one step further |
| Why is each thing here? | Route once in the header; rows show only what differs; the inspector's "Why it's here" |
| Where did I come from? | Trail path, the map's "◁ from step N" marker, U |
| What will a branch do? | The move menu names the relationship and its time; the new step opens; the music keeps playing |

### 0.3 Gaps the check found, and where they are closed

| Gap in the sources | Closed by |
|---|---|
| Where "Nothing kept yet" lives, and where "kept in step 2" shows | D10: a kept list in the trail column |
| What Retry does to a snapshot | D17 |
| What "shows the difference" means for a re-run | D16 |
| Toasts can hide the focus ring at the bottom of the screen | D27 |
| The CRT overlay paints over the video | D28 |
| What X does to the playing track; what K does to a kept track | D13, D14 |
| The parent of a Shift+M step | D19 |
| Marks on a step you return to after judging or hearing elsewhere | C2 |
| Move buttons outside the inspector against "no click starts a move" | C1 |

---

## 1. Words on screen

| Word | Means | Notes |
|---|---|---|
| dig | A named, saved tree of steps | Named after its first seed (D7). No rename in milestone 1 |
| step | One move's result: a header and a list of tracks | Numbered in creation order within the dig, never renumbered (D8) |
| move | "Sounds like this track / its label / its artist / a credited person / these keeps", near or one step further | Always worded "sounds like" |
| one step further | The move plus a second batch from the labels, artists and people of its first picks | Slower, about 30 s |
| kept, passed | Your two judgments | Global per track; latest wins |
| heard | Played for 30 s anywhere | Set once per track |
| also in step N | The same track is in another step | |
| List, Map | The two views of the Dig page | "Map" in the territory sense is worded "The whole dig, drawn as a map"; the word "territory" never appears on screen |

---

## 2. Shell

### 2.1 Vertical stack (every page)

`.device` stays a full-height flex column. Top to bottom:

| Band | Height | Shrinks? |
|---|---|---|
| Title strip | 30 px | Hidden only when the viewport is under 400 CSS px tall (only reachable at high zoom) |
| Faceplate (the player) | 224 px: video frame 204 + 2 × 8 padding + 2 × 2 border | Never |
| Width notice, below 1100 px only | One line, about 28 px | Never |
| Stage (the page) | All remaining height | Yes: scrolls inside itself; `min-height: 0` |
| Key strip, Dig page only | 36 px (`--tap`) | Never |

At up to 200% browser zoom the faceplate and key strip stay on screen; the stage absorbs every loss of height. Type uses rem (16 px root); the video frame and bands use px.

The HUD counters (`#hud`) and the LCD status lights (KEY, YTM, DEEP, JOB) are removed from the shell (D36). The scrolling marquee is removed.

### 2.2 Title strip

Unchanged markup and look: grips, "COSINE·DIGGER" brand, server LED, version, CRT toggle. The server LED keeps its states (`ok` → `--lcd`, `down` → `--red`) and gains the text alternative `S-SRV-LED-*`.

### 2.3 Faceplate anatomy (the player)

```
+-- faceplate -------------------------------------------------------------------------------+
| +--------+  +-- LCD -------------------------------------------+  +-- right block 280 ----+ |
| | video  |  | L1  ▶ X-102 - Titan                  2:41 / 6:58 |  | spectrum 280 x 56     | |
| | 200x200|  | L2  from step 2: sounds like the label Ilian Tape|  | [ LIST | MAP ]        | |
| | iframe |  | L3  (note line: refusals, offline, step end)     |  | [DIG] [FEEDS] [SETUP] | |
| |        |  +--------------------------------------------------+  +-----------------------+ |
| +--------+  |◀◀  ▶  ▶▶|    −15s  +15s  +60s                                               |
+--------------------------------------------------------------------------------------------+
```

Grid: `grid-template-columns: 204px minmax(0, 1fr) 280px`, gap 10 px, padding 8 px, faceplate bevel and gradient unchanged.

**Video window.**
- A 200 × 200 CSS px YouTube IFrame player inside a 2 px `--bevel-in` frame (204 × 204). The same size at every width (D33). Its container height sets the faceplate height.
- Nothing is positioned over the iframe: no text, buttons, tooltips, menus, toasts, sheets, rail overlays or CRT scanlines (D28: the video frame stacks above the `.crt` layer).
- The iframe has `tabindex="-1"` and sits outside the page's tab order.
- Player parameters: YouTube's own controls on (`controls=1`, which gives scrubbing; there is no custom scrub bar, D34), `autoplay=0`, `playsinline=1`, `rel=0`, `enablejsapi=1`, `origin` set to the page origin. Don't send `no-referrer`; keep the browser's default Referer.
- The player is created at boot with no video and is mounted once; route changes never touch it. Before the first track it shows YouTube's empty black player. If the IFrame API can't load, the frame holds a static placeholder (not an overlay, since no iframe exists), `S-VID-NOSIGNAL` in `--f-lcd`, `--lcd-dim` on `--lcd-bg`.

**LCD.** The existing `.lcd` box (`--lcd-bg`, `--bevel-in`, scanline gradient). Three lines, all `--f-lcd`:

| Line | Size | Content | Colour |
|---|---|---|---|
| L1 | 1.625rem | State glyph, "Artist - Title" (ellipsis, full text in `title`), right-aligned time "m:ss / m:ss" (h:mm:ss at 1 h or more), tabular numerals | `--lcd` with `--lcd-glow`; paused `--lcd-soft`, no glow |
| L2 | 1.25rem | Where the playing track came from: `S-LCD-FROM` or `S-LCD-FROM-DIG` | `--lcd-soft` |
| L3 | 1.25rem | The note line: `S-LCD-REFUSED`, `S-LCD-UNAVAIL`, `S-LCD-NOVIDEO`, `S-LCD-OFFLINE`, `S-LCD-RESTORED`, `S-LCD-GESTURE`, `S-LCD-WAITING`, `S-LCD-UPNEXT`, `S-LCD-END`, with inline buttons | Text `--st-warning` with a "!" glyph for refusals and offline; otherwise `--lcd-soft` |

- L1 is a button with the accessible name "Go to the playing track". Clicking it does what G does. It's disabled while idle.
- There is no marquee: long text truncates with an ellipsis (D35).
- LCD buttons ("Open in YouTube Music", where-next items) are `.btn--small`, at least 36 px tall (`--tap`), sitting on L3.

LCD states:

| Player state | L1 | L2 | L3 |
|---|---|---|---|
| Idle, nothing loaded | `S-LCD-IDLE` in `--lcd-soft` | In a dig: `S-LCD-IDLE-HINT-NOSEL` until a track is selected, then `S-LCD-IDLE-HINT` (P16); elsewhere empty | empty |
| Loading | "▶ LOADING  {Artist - Title}" | from line | empty |
| Playing | "▶ {Artist - Title}" · time | from line | empty or `S-LCD-UPNEXT` |
| Paused | "❚❚ {Artist - Title}" · time, `--lcd-soft`, no glow | from line | empty |
| Refused (YouTube error 101 or 150) | "! {Artist - Title}" | from line | `S-LCD-REFUSED` + "Open in YouTube Music" |
| Unavailable (error 100, 2 or 5) | "! {Artist - Title}" | from line | `S-LCD-UNAVAIL` + "Open in YouTube Music" |
| No video id | "! {Artist - Title}" | from line | `S-LCD-NOVIDEO` + "Search YouTube Music" |
| Waiting for streaming picks | "■ {last Artist - Title}" | from line | `S-LCD-WAITING` |
| Step queue ended | "■ {last Artist - Title}" · "{dur} / {dur}" | from line | `S-LCD-END` + where-next buttons (§5.2) |
| Restored after reload | "❚❚ {Artist - Title}" · saved time | from line | `S-LCD-RESTORED` |
| Offline (API or network unreachable) | last track or `S-LCD-IDLE` | empty | `S-LCD-OFFLINE` |

**Transport.** A row under the LCD, `.icon-btn` style, each at least 36 × 36 px:

| Button | Visible | Accessible name | Same as |
|---|---|---|---|
| Previous | "\|◀◀" | "Previous track" | `[` |
| Play or pause | "▶" or "❚❚" | "Play" or "Pause" | Idle: plays the selection from its row; with nothing selected in a step, plays the step from the top (P); on the new-dig screen with nothing loaded, disabled. Loaded: toggles pause |
| Next | "▶▶\|" | "Next track" | `]` |
| Seek back | "−15s" | "Back 15 seconds" | Left |
| Seek forward | "+15s" | "Forward 15 seconds" | Right |
| Seek far | "+60s" | "Forward 60 seconds" | Shift+Right |

Seek and previous or next are disabled (`aria-disabled`, 50% opacity, never colour alone: the button keeps its label) while idle.

**Right block** (280 px column, three rows, gap 8 px):
1. Spectrum: the existing canvas, widened to 280 × 56 and `aria-hidden="true"`. It's decorative: it animates only while the player state is `playing`, and holds flat bars otherwise. With reduced motion it shows a static bar pattern while playing (D30).
2. List/Map toggle: two segmented buttons "List" and "Map" with `aria-pressed`, uppercase `--f-ui`, styled like `.mode-btn` (pressed = inset bevel, `--lcd` text, LED lit). It's shown on the Dig page only. On the new-dig screen it's visible but disabled with the title `S-LISTMAP-OFF` (D29, as frame F1 shows it).
3. Modes: "Dig", "Feeds", "Setup" as `.mode-btn` links with LED and `aria-current="page"`. Dig goes to the last Dig route you had (dig, step, selection, view), or to the new-dig screen if none (D31).

### 2.4 Stage per width

| Viewport width | Stage grid (columns, gap 8 px) | Right column | Notes |
|---|---|---|---|
| ≥ 1440 px | Trail 260 · centre `minmax(0,1fr)` · right 360 | Map 300 px tall, inspector fills the rest and scrolls | |
| 1280–1439 px | Trail 240 · centre · right 320 | Map 280 tall, inspector below | |
| 1100–1279 px | Rail 56 · centre · right 300 | Map 260 tall, inspector below | The rail expands to a 240 px overlay on hover or focus (§3.2) |
| < 1100 px | The 1100–1279 grid; the centre shrinks to a 320 px minimum, then the stage scrolls horizontally | As 1100–1279 | The width notice line `S-NARROW` shows under the faceplate; the spectrum hides and the right block shrinks to its buttons (min 200 px). Usable, no guarantees (D32) |

Territory view (V): the centre becomes the canvas; the right column keeps its width (360, 320 or 300, so always at least 240) and holds the slim list in the map's slot (same height) with the inspector below. The inspector doesn't move between views.

Feeds and Setup use the full stage as one scrolling page (`.view--scroll`), unchanged in structure.

### 2.5 Key strip and toasts

**Key strip** (Dig page only, D24): one 36 px line under the stage, `--panel` on `--bevel-in`, `--f-ui` 0.8125rem with `kbd` chips. It shows the four hints that matter for the focused region, then "? keys" at the right, which is a button that opens the sheet. Contents per region are in `S-KS-*`. The K hint follows the judging target: "K keep the playing track" while the player isn't idle, otherwise "K keep the selected track", and it's dropped when there's neither.

When the video iframe has focus (window blur with `document.activeElement` = the iframe), the strip shows `S-KS-IFRAME` instead.

**Toasts on the Dig page** render inside the key strip line, replacing the hints on the left for the toast's life; "? keys" stays (D27). A toast lasts 5 s, or 8 s if it has an Undo button, and pauses while hovered or focused. Because the strip is part of the layout, a toast never covers the focus ring, a row or the player. Error toasts use a 2 px `--st-error` left border and a "!" glyph; others use `--lcd-soft`. Every toast is also announced in `#announcer` (`aria-live="polite"`).

**Toasts on Feeds and Setup** keep today's floating `.toasts` tray, bottom centre. Those pages set `scroll-padding-bottom: 96px` so a focused control scrolls clear of the tray.

### 2.6 Overlay rules

Overlays open inside the stage only and never extend over the faceplate or the key strip. They clamp to the stage rectangle, flipping above their anchor when there's no room below: the move menu, pickers, the dig switcher, the ? sheet, map tooltips, the expanded rail, seed results. A rail overlay closes before focus can land under it. No overlay intersects the iframe rectangle (criterion 8).

---

## 3. Screens

Routes: `#/dig/new`, `#/dig/{dig}/step/{step}?sel={point}&view=list|map`, `#feeds`, `#setup`. `sel` is `track:{key}`, `label:{id}`, `person:{id}`, `origin`, or absent; values are URL-encoded. Old hashes redirect: `#map` → the opening route below, `#playlists` → `#feeds`.

**Opening route** (no hash): your last viewed dig and step if any dig exists, otherwise `#/dig/new` (D9).

### 3.1 New dig (`#/dig/new`)

The stage holds one panel, full width, content column at most 960 px wide, left-aligned with 24 px padding. No trail or right column. The key strip shows `S-KS-SEARCH`. On arrival, focus goes to the seed input.

Top to bottom:

1. **Panel bar:** title `S-SB-TITLE` (`.panel__title`, `--f-pixel`) and aside `S-SB-ASIDE`. With no digs yet, the lede `S-SB-LEDE` sits under the bar (§09's "Start from a track you love").
2. **Seed box:** one bevelled input row (`--lcd-bg`, `--f-lcd` 1.375rem, `--lcd` text, like `.finder__input`). Chips sit inline before the text field; a `kbd` "/" hint at the right end. It's a `role="combobox"` with `aria-controls` on the results list.
   - Placeholder: `S-SB-PH-0` with no chips, `S-SB-PH-N` with chips.
   - **Chip:** "{Artist - Title}" (or a link's short form, or an album or label name), at most 280 px with an ellipsis and the full text in `title`, then a "×" button (accessible name `S-SB-CHIP-X`, at least 24 × 24 inside a 36 px tall chip). A library chip that isn't on cosine.club starts with an "!" glyph in `--st-warning` and has the title `S-SB-CHIP-NOTIDX`.
3. **Results** (`role="listbox"`): an inline region under the box that pushes content down rather than floating over it. It appears once the text has 2 or more characters.
   - Section "On cosine.club" (`S-SB-SEC-COSINE`): up to 8 rows "Artist - Title", then `S-SB-MORE` if there are more. While searching, the header shows `S-SB-SEARCHING` with "~" in `--st-pending`. If nothing matches: `S-SB-NOMATCH` in `--st-warning` with "!". When cosine.club doesn't return results, one "!" line per cause (public onboarding, step 3): no key `S-SB-NOKEY` + ADD KEY, a refused key `S-SB-KEY-REJECTED` + FIX KEY (both open the key screen, `#connect`), no answer `S-SB-UNREACHABLE` (+ `S-SB-LIB-STILL` only when a library exists), a 429 `S-SB-BUSY` and a 5xx `S-SB-SERVER-ERR` counting down the wait and searching again once at zero (then `S-SB-BUSY-NOW` / `S-SB-SERVER-ERR-NOW` + TRY AGAIN), and a failure inside Digger `S-SB-DIGGER-ERR`. Start a dig without a usable key creates no dig and shows `S-SB-DIG-NOKEY` + ADD KEY (or `S-SB-KEY-REJECTED` + FIX KEY) under the box.
   - Section "In your library" (`S-SB-SEC-LIB` + aside `S-SB-SEC-LIB-ASIDE`): up to 8 rows, best match first: the artist's own tracks, then exact titles, then artists and titles that start with the search, then other artist or title matches, with album-only matches last (decided 3 Oct 2026). Each row is "Artist - Title" plus a second line: `S-SB-LIB-IDX` or `S-SB-LIB-NOTIDX` for scanned files, or the `-IMP` variant naming the importer for imported rows ("in your Plex library"). If nothing matches: `S-SB-LIB-NOMATCH`. With no library (the `tracks` table is empty): the section is replaced by one quiet line, `S-SB-NOLIB`, where "Setup" links to `#setup`.
   - A pasted Discogs, YouTube or cosine.club URL gives one row at the top: `S-SB-LINK-ROW`.
   - Rows already added as chips show `S-SB-ADDED` and are inert.
   - Library matches render as soon as they arrive and don't wait for cosine.club. A response for an older query is dropped. The implementer may split the call (for example `source=library|cosine` on `GET /api/search`) to get this.
   - Highlight: the first row is highlighted (R6: Enter takes the top match). Up and Down move through rows in visual order (cosine.club, then library); hover moves the highlight too. Highlighted row: `--panel-3` background and a 4 px `--mark-selected` left bar.
4. Under the results: `S-SB-LINKS` (`--muted`).
5. **Start row:** the `.btn--go` button `S-SB-BTN`, disabled with 0 chips, followed by the hint `S-SB-BTN-HINT` (`--muted`). While starting: the button reads `S-SB-STARTING`, disabled. On failure, `S-SB-START-ERR` appears as an `.alert` under the box and the chips stay.
6. **Recent digs** (hidden when there are none): heading `S-SB-RECENT`, up to 5 rows, newest first. Each row is the dig name (bold, `--white`) and `S-SB-RECENT-META` (`--muted`). A row is a link to that dig's last viewed step.
7. **Today's Daily** (only if the Daily ran today and has a playlist URL): a small card titled `S-SB-DAILY`, with `S-SB-DAILY-META`, "▶ Open in YT Music" (`.btn--yt`, external) and the link `S-SB-DAILY-FEEDS` to `#feeds`. It isn't openable as a dig (D11).

Seed-box behaviour (all in the text field):

| Input | Result |
|---|---|
| Typing | Results after a 250 ms pause; 2 characters minimum |
| Enter, text present, a row highlighted | Adds it as a chip, clears the text, keeps focus |
| Enter, text looks like a URL | Adds a link chip at once |
| Enter, text present, no rows yet | Nothing is added; the hint `S-SB-ENTER-WAIT` shows under the box |
| Enter, empty text, 1 or more chips | Starts the dig (`POST /api/digs`) |
| Enter, empty text, no chips | Hint `S-SB-EMPTY-ENTER` |
| Backspace, empty text | Removes the last chip; announces `S-SB-REMOVED` |
| Up, Down | Move the highlight |
| Esc | Closes the results; a second Esc clears the text; a third blurs the field to the page |
| Adding a duplicate | No new chip; hint `S-SB-DUP` |
| Adding an 11th chip | No new chip; hint `S-SB-MAX` (10 seeds at most, the engine's seed budget, D12) |

Hints are one line under the box, `--muted`, cleared by the next keystroke. Clicking or double-clicking a result adds a chip (§07). Clicking a chip's "×" removes it. After a successful start the route becomes `#/dig/{dig}/step/{root}` and the new step opens running (§3.3).

### 3.2 In a dig: trail (left)

Panel `--panel`, `--bevel-in`, scrolls on its own. Top to bottom:

1. **Dig header:** the dig name (`--f-ui` 0.875rem, uppercase, `--white`, ellipsis) with a "▾" switcher button (accessible name `S-TR-SWITCH`). Under it: `S-TR-META` (`--muted`).
   - The switcher opens a menu: `S-TR-NEW` (to `#/dig/new`), then up to 10 recent digs as `S-TR-SWITCH-ROW`. Picking one opens that dig's last viewed step.
2. **Tree** (`role="tree"`, one tab stop, roving focus): rows for the path to the viewed step expanded, other branches collapsed to stubs (R4).
   - **Step row** (36 px), left to right:
     - a 4 px left bar (viewing only)
     - the step number (`--f-lcd` 1.25rem, `--amber`)
     - the name: root `S-TR-ROOT-1` or `S-TR-ROOT-N`; other steps the origin's short name (label name, artist name, track title, person name, or `S-TR-THESE`), ellipsis
     - a kind tag: "track", "label", "artist", "person" or "keeps" (`--muted`, 0.75rem)
     - "»" if one step further (title "one step further")
     - the marker cluster, right-aligned
   - Indent: 12 px per level, up to 4 levels; deeper rows stop indenting and show "↳" before the number.
   - **Marker cluster** order: ▶ (holds the playing track), then the judgment counts "★{k} ✕{p}" (omitted at 0), then the status: "~ {i}/{n}" running, "{count}" done, "! failed", "‖ stopped". "changed" follows the name as a tag on re-run steps.
   - **Stub row:** `S-TR-STUB` under the parent where a hidden branch leaves the path. Click or Enter expands that branch. Expanded stubs stay expanded until reload (D25).
   - Hovering a step row shows the tooltip `S-TR-TIP`.
3. **Kept in this dig** (D10): a disclosure button `S-TR-KEPT-H` (collapsed by default, state remembered per dig in local storage).
   - Expanded, it lists this dig's keeps, newest first, as `S-TR-KEPT-ROW` links.
   - Clicking one opens that step with the track selected. It's navigation, never a move.
   - With no keeps: `S-TR-KEPT-EMPTY` (`--muted`).
4. **Legend** pinned at the bottom: `S-TR-LEGEND` (`--muted`, 0.75rem).

**Rail (1100–1279 px).** 56 px wide. One cell per visible tree row: the step number, and under it a single marker glyph (▶ before ~ before ! before ‖), plus "+" for stubs. The viewing cell gets the left bar. Dig header, kept list and legend are hidden in the rail.

On pointer enter or focus entering, the rail opens as a 240 px overlay over the centre (`--panel`, `--bevel-out`, `--hard-shadow`) with the full trail. It closes at once on pointer leave or focus leave. It expands with a 120 ms width transition, or instantly with reduced motion.

### 3.3 In a dig: the step (centre)

Panel `--panel`, `--bevel-in`. A sticky header, then the scrolling list.

**Step header.** Rows, top to bottom:

| Row | Content | Style |
|---|---|---|
| H1 | "{N}" + the sentence (`S-HDR-*`), uppercase via CSS; right side: status tag (`S-HDR-TAG-*`) | `--f-ui` 0.875rem, `--white`; number `--f-lcd` `--amber` |
| H2 | Meta `S-HDR-META` (or `S-HDR-META-RETURN` when returning, D20); right side: buttons "▶ Play step" (P) and "Re-run" (`.btn--small`); "Re-run later as step {m}" link (`S-HDR-RERUN-LATER`) when one exists | `--muted` 0.8125rem |
| H3 | Origin line `S-HDR-FROM-*`; on root steps with several seeds the seeds are in H1, so H3 is omitted | `--muted` |
| H4 | Notes `S-N-*`, one per line, each with a leading "!" | `--st-warning` text on `--panel-2`, with an inline button where the note offers one |
| H5 | While running: `S-HDR-PROGRESS` and a 6 px bar (`--st-pending` fill on `--line`) | `--muted` |
| H6 | Re-run only: `S-HDR-RERUN` | `--st-info` text with a "+" glyph |

The header carries the route once (R17). Rows don't repeat it.

**Rows** (one tab stop, roving; Up and Down move the selection). Each row is 40 px tall; rows in a step with several seeds, and rows in a further batch, are 56 px tall because they carry a why line.

```
| bar | rank | nearness | Artist - Title ............ | marks  or  [▶ Play][★ Keep][✕ Pass][Sounds like ▾] |
|     |      |          | why line (several seeds, or further batch)                                     |
```

- **Rank cell:** 28 px, `--f-lcd` 1.25rem, `--amber`, right-aligned. It shows "▶" in `--mark-playing` while this row plays, "❚❚" while paused, "~" while loading, and "!" in `--st-warning` after a refusal. The rank stays in the accessible name.
- **Nearness bar:** 40 × 6 px, fill = score × 40 px in `--node-pick` on `--line`, `aria-hidden`. Score is in the row's `title`: `S-ROW-TITLE`.
- **Title:** "Artist - Title", `--f-body` 0.9375rem, `--text`, one line with an ellipsis; full text in `title` and the inspector.
- **Marks:** right-aligned, at most 3, joined by " · ", overflow "+{n}" with the rest in `title`. Priority: refusal (`S-ROW-REFUSED` / `S-ROW-UNAVAIL` / `S-ROW-NOVIDEO`), kept or passed, new, heard, also in, also on. Strings are `S-ROW-*`. Playing and paused live in the rank cell, so they never get hidden.
- **Why line** (0.8125rem `--muted`): `S-ROW-WHY-*` in steps with several seeds, and `S-ROW-WHY-FURTHER` in a further batch.
- **Buttons** show on hover and on the selected row, replacing the marks area; row height doesn't change:
  - "▶ Play"
  - "★ Keep", or "★ Kept" with `aria-pressed="true"` when kept
  - "✕ Pass", or "✕ Passed" with `aria-pressed="true"`
  - "Sounds like ▾"

  Each is `.btn--small`, 36 px tall. They're `tabindex="-1"`: the keyboard routes are Space, K, X, Enter and the inspector's buttons (D23).

**Batch divider** (one step further): a full-width 32 px row with `S-ROW-DIVIDER` (`--f-ui` uppercase `--muted`, dotted `--line` rules either side). While that batch streams, `S-ROW-DIVIDER-RUN` follows it. Ranks continue across the divider (26, 27, …).

**Re-run gone section:** after the last row, a divider `S-ROW-GONE-DIVIDER`, then the tracks the original had that this run lacks. They're muted, selectable and playable one at a time (Space plays only that row). They aren't in the step's queue, and they show their own marks (D16).

**Body states** (they replace or follow the rows):

| State | Body |
|---|---|
| Running, 0 rows | `S-HDR-PROGRESS` in H5; body empty (a label, artist or person move reads `S-HDR-RESOLVING` until its first progress note; interaction-spec O14.5) |
| Running, rows arriving | Rows append in rank order and never reorder; each new row fades in over 200 ms (none with reduced motion) |
| Waiting for another move (if the server serialises) | H5 reads `S-HDR-WAITING` |
| Done, 0 picks | `S-E-EMPTY` + two move buttons (`S-E-EMPTY-BTN-*`, `.btn`), chosen by the rule in §5.4 |
| No seed resolved | `S-N-NONE` in H4 + "Choose a different seed"; body empty |
| Failed | Received rows stay. Below them a block: `S-E-FAIL-*` (first line, `--st-error`, "!"), `S-E-FAIL-L2`, and the retry button (`S-E-RETRY*`, D17) |
| Stopped | Received rows stay. Block: `S-E-STOPPED` (`--muted`, "‖") + "Retry" (0 picks) or "Re-run" (1 or more) |
| Needs a Discogs token | `S-E-NOTOKEN-*` + link "Setup" |

### 3.4 In a dig: the step map (right, top)

A panel with a bar titled `S-MAP-TITLE` and the icon buttons "−", "+", "Fit" (36 px, `tabindex="-1"`; keys `-`, `+`, `0` do the same when the canvas has focus, D26). Under the bar sits the canvas (300, 280 or 260 px tall), then the legend line `S-MAP-LEGEND` (0.75rem `--muted`; it may wrap to 2 lines below 1440 px).

Status captions sit in the canvas's top-left corner as HTML text inside the map panel, never over the player:
- `S-MAP-NOHUB` when the step has picks but no hubs
- `S-MAP-DISCOGS-DOWN` in `--st-warning`

Without a Discogs token the map shows `S-MAP-NOHUB` and no token line (P8): Setup's Discogs card is where a token is asked for.

Grammar and marks: §7.

### 3.5 In a dig: the inspector (right, below the map)

Panel `--panel`, scrolls. The bar title says what is shown: `S-INS-K-TRACK`, `S-INS-K-LABEL`, `S-INS-K-PERSON`, `S-INS-K-ORIGIN` or `S-INS-K-STEP`. Sections use `.field__label` headings. Body `--f-body` 0.875rem.

**A pick is selected:**
1. **Title block:** "Artist - Title" (wraps, `--white`), with a 56 × 56 Discogs thumbnail at the left when known (D39). Buttons:
   - "▶ Play" (or "❚❚ Pause" when it's the playing track)
   - "★ Keep" / "★ Kept"
   - "✕ Pass" / "✕ Passed"
   - "Open in YouTube Music ↗": `https://music.youtube.com/watch?v={video_id}`; with no video id it becomes "Search YouTube Music ↗" (`https://music.youtube.com/search?q={Artist Title}`)
2. **Why it's here** (`S-INS-WHY-H`): `S-INS-WHY-1`, `S-INS-WHY-N` or `S-INS-WHY-FURTHER`. This is the move and its score, never a fact.
3. **Also in** (`S-INS-ALSO-H`, only if the track is in other steps or heard): `S-INS-ALSO-ROW` per step (a link: opens that step with this track selected), then `S-INS-HEARD`.
4. **Facts** (`S-INS-FACTS-H` + aside `S-INS-FACTS-ASIDE`): states `S-INS-FACTS-LOADING`, `S-INS-FACTS-MISSING`, `S-INS-FACTS-DOWN`, `S-INS-FACTS-NOTOKEN`. When loaded, a two-column list:
   - "Artist": {artist} + `S-INS-SOUNDS` button (A)
   - "Label": {label} + `S-INS-SOUNDS` (L); one row per label
   - "Year": {year}
   - "Credits": {person} ({role}) + `S-INS-SOUNDS` (C); up to 6 rows, then `S-INS-CREDITS-MORE`
   - "Style": {styles joined by ", "}
   - `S-INS-ALSO-ON` above the list when the pick really is on this step's label (R12)
   - the link `S-INS-DISCOGS` to the release

   Clicking a label or person name selects that label or person (the inspector then shows it, and the map marks its hub if one exists). Year and style have no "Sounds like" (D40).

**A hub is selected:** title "■ {Label}" or "◯ {Person}"; `S-INS-HUB-LABEL` or `S-INS-HUB-PERSON`; `S-INS-HUB-ALSO` lines; the list of picks it's on (links that select the pick); buttons `S-INS-SOUNDS` and `S-INS-SOUNDS-FAR`.

**A label or person without a hub is selected** (from a fact click): the same as a hub, with `S-INS-HUB-*` reading 1 pick and no map ring.

**The origin is selected** (map centre): title `S-INS-ORIGIN-H`. One row per origin point (seed steps: `S-INS-ORIGIN-SEED`); the link `S-INS-ORIGIN-FROM` to the parent. For a single-track origin it also has the buttons "Sounds like its label", "Sounds like its artist" and "Sounds like a credited person"; T on the origin gives `S-T-SAMEMOVE` (D21).

**Nothing is selected: the step summary.**
- The sentence (`--white`)
- the origin line
- `S-INS-SUM-COUNTS`
- `S-INS-SUM-HUBS`, or `S-MAP-NOHUB`
- `S-INS-SUM-FACTS` while facts load
- the move buttons `S-INS-SUM-M` and `S-INS-SUM-SHIFTM`, shown when their keep count is 1 or more; when this step holds every keep of the dig they are the same move, shown once as `S-INS-SUM-KEEPS`
- the hint `S-INS-SUM-HINT`

### 3.6 Territory view (V)

The route keeps its step and selection; only `view=map` changes, and the list becomes the canvas. The key strip shows `S-KS-TERR`.

- **Centre:** a panel titled `S-TERR-TITLE` with "−", "+", "Fit" in the bar, and the canvas filling the rest. The open island is the route's step. Grammar: §7.2.
- **Right column:** the slim list in the map's slot, then the inspector.
  - Slim list: heading `S-TERR-LIST-H`, rows 36 px: rank cell, title (ellipsis), one mark. Hover or selection shows icon buttons "▶" "★" "✕" (accessible names "Play", "Keep", "Pass"; 36 px; `tabindex="-1"`).
  - The same selection, judging and Enter-menu rules as the list.
- Clicking an island opens that step and keeps the view on Map. Clicking a dot in the open island selects it. Clicking a hub selects it and highlights its other appearances. Clicking empty canvas clears the selection; double-clicking empty canvas fits the whole dig.

### 3.7 Move menu and pickers

**Move menu** (Enter on a selection, or "Sounds like ▾" on a row). A `role="menu"` popover anchored under the selected row, or beside the selected dot or hub. It's `--panel-2` with a `--lcd-soft` 2 px border and `--hard-shadow`, 360 px wide, clamped to the stage. Focus moves into it.

```
SOUNDS LIKE…
from: Choplin - Netlabels Mix 06.06
  T  this track                                   about 3 s        [» further]
  L  its label: Ilian Tape                         about 10 to 30 s [» further]
  A  its artist: Choplin                           about 4 to 8 s   [» further]
  C  a credited person ▸                           about 10 to 30 s [» further]
Shift + key: go one step further, slower (about 30 s)
```

- Strings: `S-MENU-*`.
- Each item is a `menuitem` with a key letter in a `kbd` chip, its label, and its estimate (`--muted`). A trailing "» further" button (accessible name `S-MENU-FAR-ARIA`) starts the further variant.
- Disabled items stay listed with their reason suffix and `aria-disabled`:
  - L: `S-MENU-LOADING` (facts loading), `S-MENU-NOLABEL`, `S-MENU-NOTOKEN`
  - C: `S-MENU-LOADING`, `S-MENU-NONE`, `S-MENU-NOTOKEN`
- Several labels or artists: the item text reads "its label ▸" or "its artist ▸" and opens the picker.
- On a hub, the menu has one item (L for a label hub, C for a person hub) and its further button. On the origin, the menu lists the origin track's L, A and C items; T is omitted.
- Keys inside the menu:
  - T, L, A, C choose near; Shift+letter chooses further
  - Up and Down move; Enter chooses near; Shift+Enter chooses further
  - Right or Enter on a "▸" item opens its picker
  - Esc or Left closes (a picker closes back to the menu)
  - Left and Right never seek while the menu is open (D26)
- Hovering an item changes nothing; the estimate is always visible.

**Picker** (several labels, artists or people; reached by L, A or C directly or from the menu): the same popover with the title `S-PICK-LABEL`, `S-PICK-ARTIST` or `S-PICK-PERSON`. Rows "1  {name}" (people add " · {role}"), up to 9. Keys 1–9 choose; Up, Down and Enter choose; Esc cancels. The Shift state of the key that opened it carries through.

### 3.8 The ? sheet

A `role="dialog"` with `aria-modal="true"`, titled `S-KEYS-TITLE`, centred in the stage (never over the faceplate), at most 720 × the stage height, scrolling inside. Focus is trapped and returns to where it was. "?" or Esc closes it. Contents are in §6.4.

### 3.9 Feeds (`#feeds`)

Replaces Playlists. Page title `S-FEEDS-TITLE`, lede `S-FEEDS-LEDE`.

Directly under the page head, a small LCD panel (the existing `.lcd` style) holds the clock and next runs, moved from the faceplate: `S-FEEDS-CLOCK` (the large clock, `--f-lcd`), and `S-FEEDS-NEXT-DAILY` and `S-FEEDS-NEXT-WEEKLY` in the `.lcd__readout` style.

The Daily and Weekly cards are unchanged: size, next run, last run with badge, "▶ Open in YT Music", "Run now", "Dry run", log, result. The "Recent digs" panel is removed: the dig switcher and the new-dig screen hold recent digs. No key strip.

### 3.10 Setup (`#setup`)

Unchanged panels: Keys, Schedule, YouTube Music, Health. The Library section gains:

1. Music folder and "Scan now", as today. The help text becomes `S-SET-LIB-HELP`.
2. **Missing labels** field: label `S-SET-FILL-LABEL`, status line, then the button `S-SET-FILL-BTN` (`.btn`) and, while running, "Stop" (`.btn`).
   - Status lines: `S-SET-FILL-IDLE`, `S-SET-FILL-NONE`, `S-SET-FILL-RUN` (with a 6 px `--st-pending` bar), `S-SET-FILL-DONE` (`--st-success` "✓"), `S-SET-FILL-STOPPED`, `S-SET-FILL-ERR` (`--st-error` "!"), `S-SET-FILL-NOTOKEN` (button disabled), `S-SET-FILL-NOLIB`.
   - The budget line `S-SET-FILL-BUDGET` sits under the button (`.field__help`).
   - The job uses the existing jobs and SSE path, survives page changes, and resumes where it stopped.

A new **About** panel (`.panel`, title `S-ABOUT-TITLE`) goes last in the right column, after Health. It holds `S-ABOUT-START` (or `S-ABOUT-SERVE` when `/api/health` says the Digger was started with `serve`, Docker included; the paths come from `/api/settings` `data`), `S-ABOUT-2` (with two links) and `S-ABOUT-3`. The links are "YouTube Terms of Service" → `https://www.youtube.com/t/terms` and "Google Privacy Policy" → `https://policies.google.com/privacy`, opening in a new tab with `rel="noopener"`.

---

## 4. Element states

Every status pairs colour with a glyph or text. Hover never changes data. "Bar" means a 4 px left bar.

### 4.1 Trail step row

| State | Glyph or text | Token(s) |
|---|---|---|
| Default | number, name, kind tag, count | `--text`; number `--amber`; tag `--muted` |
| Hover | tooltip `S-TR-TIP` | `--panel-2` background |
| Viewing (route step) | bar, `aria-current="true"` | bar `--mark-selected`; `--panel-3` background |
| Keyboard focus | 3 px outline, offset 2 | `--focus` |
| Running | "~ {i}/{n}" | `--st-pending` |
| Holds the playing track | "▶" | `--mark-playing` |
| Has keeps or passes | "★{k}" "✕{p}" | `--mark-kept`, `--mark-passed` |
| Done | "{count}" | `--muted` |
| Failed | "! failed" | `--st-error` |
| Stopped | "‖ stopped" | `--muted` |
| Changed (re-run) | tag "changed" | `--st-info`, 1 px outline |
| One step further | "»" | `--muted` |

### 4.2 Trail stub, kept list, switcher

| Element | State | Glyph or text | Token(s) |
|---|---|---|---|
| Stub | default / hover / focus | `S-TR-STUB` | `--muted` / `--panel-2` / `--focus` |
| Kept list toggle | collapsed / expanded | "▸" / "▾" + `S-TR-KEPT-H` | `--text` |
| Kept list | empty | `S-TR-KEPT-EMPTY` | `--muted` |
| Kept row | default / hover | "★" + `S-TR-KEPT-ROW` | ★ `--mark-kept`; hover `--panel-2` |
| Switcher | open | menu with `S-TR-NEW` first | `--panel-2`, `--hard-shadow` |

### 4.3 List row

| State | Glyph or text | Token(s) |
|---|---|---|
| Default | rank, bar, title, marks | `--text` on `--panel` |
| Hover | buttons replace marks; its dot ringed on the map | `--panel-2` |
| Selected | bar, 1 px dashed inset outline, buttons shown, `aria-selected="true"` | `--mark-selected`; `--panel-3` |
| Keyboard focus | 3 px solid outline outside the row (separate from the selection's dashed inside outline) | `--focus` |
| Loading (player) | rank cell "~" | `--st-pending` |
| Playing | rank cell "▶" | `--mark-playing` |
| Paused | rank cell "❚❚" | `--lcd-soft` |
| Refused / unavailable | rank cell "!" + mark `S-ROW-REFUSED` / `S-ROW-UNAVAIL` | `--st-warning` |
| No video | mark `S-ROW-NOVIDEO` | `--muted` |
| Kept | "★ kept", or `S-ROW-KEPT-IN` when judged in another step | `--mark-kept` |
| Passed | "✕ passed" / `S-ROW-PASSED-IN`; title struck through | `--mark-passed` |
| Heard | `S-ROW-HEARD` or `S-ROW-HEARD-IN`; title dims | title `--muted` |
| Also in | `S-ROW-ALSO` | `--muted` |
| Also on (really on the move's label) | `S-ROW-ALSOON` | `--node-label` text + "■" |
| New (re-run) | "+ new" | `--st-info` |
| Gone (re-run section) | row in `--muted`, original marks kept | `--muted` |

### 4.4 Step header

| State | Tag (H1 right) | Token(s) |
|---|---|---|
| Running | "~ running" + H5 progress | `--st-pending` |
| Waiting | "~ waiting" + `S-HDR-WAITING` | `--st-pending` |
| Done | none | — |
| Done, returning | H2 `S-HDR-META-RETURN` | `--muted` |
| Failed | "! failed" + failure block | `--st-error` |
| Stopped | "‖ stopped" + stopped block | `--muted` |
| Changed | "changed" + H6 | `--st-info` |
| Note present | H4 lines with "!" | `--st-warning` |
| Empty (0 picks) | none; body `S-E-EMPTY` | `--muted` |

### 4.5 Map nodes (step map and the open island)

| Node, state | Drawing | Token(s) |
|---|---|---|
| Pick, default | Filled dot, 8 px diameter | `--node-pick` |
| Pick, arriving | Fades in over 200 ms; none with reduced motion | — |
| Pick, hover | Ring 16 px diameter + tooltip `S-MAP-TIP-PICK`; its row and hub lines highlight | `--text` |
| Pick, selected | Dashed square 18 × 18, dash 3/2, 1.5 px; its lines brighten to full alpha; everything else dims to 40% | `--mark-selected` |
| Pick, playing | Pulsing ring (radius 10 → 16, alpha 1 → 0, 1.4 s loop); reduced motion: static double ring at radii 9 and 12 | `--mark-playing` |
| Pick, kept | Ring at radius 7 + a 9 px "★" above right | `--mark-kept` |
| Pick, passed | Hollow dot, 1.5 px stroke | `--mark-passed` |
| Pick, heard | Dot at 55% alpha | `--node-pick` |
| Label hub | Filled square 12 × 12 + name (0.75rem `--text`) | `--node-label` |
| Person hub | Hollow circle 12 px diameter, 2 px stroke, `--panel` fill + name | `--node-person` |
| Hub, hover | Tooltip `S-MAP-TIP-LABEL` / `S-MAP-TIP-PERSON`; its picks highlight | — |
| Hub, selected | Dashed square around it; its lines and picks at full alpha; others 40% | `--mark-selected` |
| Hub line | 1 px solid from hub to each of its picks, 45% alpha | hub's colour |
| Origin (one point) | "★" 14 px at the centre | `--node-seed` |
| Origin, running | Expanding "listening" ring, 1.6 s loop; reduced motion: a static dashed ring | `--st-pending` |
| Origin, selected | Dashed square | `--mark-selected` |
| Seeds (several, D4) | One "★" per seed on an inner ring of 18 px radius at its sector's middle angle; sector bounds as 1 px dotted radial lines; seed short name (16 characters max) at the rim | `--node-seed`; lines `--line`; names `--muted` |
| From marker | `S-MAP-FROM` at the top-left rim + dotted line to the origin; click opens the parent (as U) | `--muted` |
| Guide rings | Dotted circles at score 0.8 and 0.6 | `--line` |

### 4.6 Territory island

| State | Drawing | Token(s) |
|---|---|---|
| Open | The full step map (§4.5) at radius 160 world units | as §4.5 |
| Collapsed | Dotted boundary circle (radius 60); origin star(s); kept picks as "★"; hubs that recur in 2 or more steps of this dig; the playing pick, if any, as a dot with its ring | boundary `--line`; ★ `--mark-kept` |
| Label | "{N} · {short name}" under the island, plus "~", "!", "‖" or "changed" | `--text`; status tokens as §4.1 |
| Hover | Tooltip `S-TR-TIP`; a recurring hub's other appearances highlight | — |
| Holds the playing track | "▶" before the label | `--mark-playing` |

### 4.7 Faceplate, seed box, Setup job

| Element | State | Glyph or text | Token(s) |
|---|---|---|---|
| LCD | idle / loading / playing / paused / refused / offline / ended | §2.3 table | `--lcd`, `--lcd-soft`, `--st-warning` |
| Transport button | default / hover / pressed / disabled / focus | label + `aria-disabled` | `.icon-btn` bevels; disabled at 50% opacity; `--focus` |
| List/Map | pressed / not / disabled | `aria-pressed`; LED | `--lcd` when pressed |
| Mode button | current / not | LED lit, `aria-current` | existing `.mode-btn` |
| Spectrum | playing / not | moving bars / flat; static under reduced motion | existing canvas colours |
| Video frame | no API | `S-VID-NOSIGNAL` placeholder | `--lcd-dim` on `--lcd-bg` |
| Server LED | ok / down | colour + `aria-label` `S-SRV-LED-OK` / `S-SRV-LED-DOWN` | `--lcd` / `--red` |
| Server-lost banner | down | `S-SRV-LOST` as a full-width line at the top of the stage, "!"; after 10 s `S-SRV-LOST-START`, or `S-SRV-LOST-SERVE` when the last health answer said `serve` | `--st-error` |
| Seed result row | default / highlighted / added | text / bar + `--panel-3` / `S-SB-ADDED` | `--mark-selected` |
| Seed chip | default / not indexed / focus on × | text / "!" glyph / outline | `--st-warning`, `--focus` |
| Search section | searching / no match / error | "~" / "!" / "!" | `--st-pending` / `--st-warning` / `--st-warning` |
| Fill-labels job | idle / running / done / stopped / error / no token | §3.10 strings | `--muted` / `--st-pending` / `--st-success` / `--muted` / `--st-error` / `--st-warning` |

---

## 5. Interaction rules

### 5.1 Selection and playback

- **One selection per step:** a pick, a hub, a label or person from a fact, the origin, or none. It's stored in the route's `sel` and remembered per step for the session, so returning restores it.
- **What changes the selection:** clicking a row, dot, hub, map centre or inspector fact; Up and Down; G; Esc (clears it); also-in and kept-list links (open that step with the track selected).
- **Following the music** (changed 2026-10-16): while a step's selection is the track that was playing, the next track to play in that step takes the selection (auto-advance, next, previous, X on the playing track), so the inspector shows what plays. Selecting anything else stops it following.
- **What never changes it otherwise:** playback, auto-advance, a step finishing in the background, the List/Map switch, starting a move (the new step opens with no selection; the old step keeps its own), toasts.
- A step you open for the first time has no selection, and the inspector shows the step summary. Up or Down with no selection selects rank 1. Up and Down stop at the ends; there's no wrap.
- Selecting from the map scrolls the list to the row (an instant jump with reduced motion). Selecting a row rings its dot.
- **G:** selects the playing track and opens its step, in its dig, keeping the current view. With nothing loaded: `S-T-NOTHINGPLAYING`.
- **Judging target:** the playing track (the current queue item) whenever the player state isn't idle (loading, playing, paused, refused, waiting); otherwise the selection, if it's a pick.

### 5.2 Player and queue

- **Queue:** an ordered list of one step's picks (its source step) and an index. Auto-advance skips passed tracks and tracks without a video id; heard and kept tracks still play. The queue follows the step live, so picks that are still streaming join its end.
- **Starting playback** (each is a user gesture, so the first play satisfies autoplay rules):

| Action | Queue becomes | Plays |
|---|---|---|
| Space on a row, dot or slim-list row; double-click a row or dot; row or inspector "▶ Play" | The selection's step, from this row to its end (D15) | Now |
| P, or "▶ Play step" | The viewed step, from rank 1 | Now |
| Shift+P | The current track continues; then the viewed step from rank 1 (the rest of the old queue is dropped); L3 shows `S-LCD-UPNEXT` | After the current track |
| Space on the playing track | unchanged | Pause or resume |
| Space on a gone row (re-run) | That one track only | Now |

- `[` restarts the track if more than 3 s in, otherwise goes to the previous queue item. `]` goes to the next playable item, or to the step-end state if there's none.
- **What never changes the queue:** starting, finishing or failing a move; navigating; List/Map; re-run; judging (except X on the playing track, D13). A new step never autoplays.
- **Streaming end:** if the queue reaches its last arrived pick while its step is still running, the player waits (L3 `S-LCD-WAITING`) and continues when the next pick arrives. If the step finishes with none, it goes to step end.
- **Step end:** the player stops. L1 shows "■" and the last track; L3 shows `S-LCD-END` and up to 3 where-next buttons from the source step's hubs, ordered by pick count:
  - "■ {Label}" (accessible name `S-LCD-WN-LABEL-ARIA`) starts the label move from that hub
  - "◯ {Person}" (`S-LCD-WN-PERSON-ARIA`) starts the person move
  - "◆ this track" (`S-LCD-WN-TRACK-ARIA`) starts "Sounds like this track" from the last played track

  With fewer than 2 hubs, the list fills up with "◆ this track" and then `S-LCD-WN-ARTIST`. These are move buttons (C1). The player state is now idle.
- **Refusal** (error 101 or 150): the row's rank cell turns "!", the row gets `S-ROW-REFUSED`, and L3 shows `S-LCD-REFUSED` with "Open in YouTube Music". At 2.0 s after the error it advances to the next playable item (or step end). The countdown in L3 updates each second. Refusals are remembered by video id for the session (D38). Errors 100, 2 and 5 do the same with `S-LCD-UNAVAIL` and `S-ROW-UNAVAIL`. No video id: `S-LCD-NOVIDEO`, skipping after 2 s.
- **Offline:** if the IFrame API script fails to load, or a load times out after 10 s with no network: L3 shows `S-LCD-OFFLINE` and the frame shows `S-VID-NOSIGNAL`. Play attempts repeat the note; "Open in YouTube Music" links keep working.
- **Blocked first play:** if the browser blocks playback (the state stays unstarted 1.5 s after `playVideo`), L3 shows `S-LCD-GESTURE`.
- **Heard:** after 30 s of actual playback of a track (time in the playing state; seeking doesn't count), the player sends `POST /api/heard` once, and the heard marks appear in every step holding that track.
- **Pages:** the player keeps playing on Feeds and Setup. L2 switches to `S-LCD-FROM-DIG` whenever the viewed page isn't the playing track's dig.

### 5.3 Judgments

| Action | Target | Result |
|---|---|---|
| K | Judging target (§5.1) | Keep. Already kept: `S-T-ALREADY-KEPT`, no change (D14). Was passed: kept, `S-T-KEPT-WAS` |
| X | Judging target | Pass. On the playing track it also skips to the next track at once (D13). Already passed: `S-T-ALREADY-PASSED` |
| Row or inspector "★ Keep" / "★ Kept" | That row | Toggles keep on and off |
| Row or inspector "✕ Pass" / "✕ Passed" | That row | Toggles pass on and off |
| Z or Cmd+Z | — | Undoes the last judgment change in this page session; repeated presses walk back further. Restores the previous state (none, keep or pass). `S-T-UNDO-*`; nothing left: `S-T-NOUNDO` |
| K or X with no target | — | `S-T-NOTARGET-K` / `S-T-NOTARGET-X` |

- Each judgment shows a toast (`S-T-KEPT`, `S-T-PASSED`) with "Undo" (same as Z), and announces itself.
- Marks update everywhere at once: rows, dots (star ring, hollow), trail counts, kept list, header meta.
- `step_id` sent with a judgment is the step the judged track was in at the time: the source step for the playing track, or the row's step.
- If the API call fails, the mark reverts and `S-T-JUDGE-ERR` shows.
- Judgments never touch the queue (except X on the playing track) or the selection.

### 5.4 Moves

**Move controls.** Steps are created only by:
- the keys T, L, A, C (with Shift for further), M and Shift+M
- move menu and picker items
- buttons whose label starts "Sounds like" or "Go one step further": inspector, empty step, step end, step summary (C1)
- the header's "Re-run" and "Retry" (§5.6)

No click or double-click on a row, dot, hub, island, trail row, fact name, chip, LCD title or empty space ever creates a step.

**Source of a move:** the selection. The playing track is never the source.

| Key | Selection is a pick | Label hub | Person hub | Origin (single track) | Nothing |
|---|---|---|---|---|---|
| T | Sounds like this track | `S-T-WRONGHUB-LABEL-T` | `S-T-WRONGHUB-PERSON-T` | `S-T-SAMEMOVE` if the step is a track move from it; otherwise the track move | `S-T-NOSEL` |
| L | Its label: one → start; several → picker; facts loading → `S-T-FACTS-LOADING`; none → `S-T-NOLABEL`; no token → `S-T-NOTOKEN-L` | Sounds like it | `S-T-WRONGHUB-PERSON` | Origin track's label, same rules | `S-T-NOSEL` |
| A | Its artist: one → start; several credited → picker | `S-T-WRONGHUB-LABEL-A` | `S-T-WRONGHUB-PERSON-A` | Origin track's artist | `S-T-NOSEL` |
| C | A credited person: one → start; several → picker; loading → `S-T-FACTS-LOADING-C`; none → `S-T-NOPERSON` | `S-T-WRONGHUB-LABEL` | Sounds like them | Origin track's people | `S-T-NOSEL` |
| Shift+key | The same, one step further | The same, one step further | The same, one step further | The same, one step further | `S-T-NOSEL` |
| Enter | Opens the move menu for the selection | menu (1 item) | menu (1 item) | menu | nothing |

- M and Shift+M ignore the selection:
  - M: kept tracks among this step's picks, wherever they were kept (D6). With none: `S-T-NOKEEPS-STEP`.
  - Shift+M: kept tracks across all steps of this dig. With none: `S-T-NOKEEPS-DIG`.
- M and Shift+M are multi-origin "these" moves (decision 7); all other moves stay single-origin. With more than 10 keeps, the 10 most recent are used and the step carries `S-N-KEEPS-CAP`. The parent is the step you're viewing for both M and Shift+M (D19).
- **Starting a move:**
  1. `POST /api/steps`.
  2. The new step appears in the trail under its parent with "~" and opens at once. That's a route push, and you asked for it.
  3. Its header shows `S-HDR-PROGRESS` at "0 of 25"; the map switches to it with the origin, the listening ring and `S-MAP-FROM`.
  4. The music keeps playing and the queue is unchanged.
  5. The announcer says `S-AN-STEP-START`.

  If the POST fails: `S-T-MOVE-ERR` and no step.
- **Duplicates** (D18): the same parent, origin, move and further as an existing step opens that step and shows `S-T-DUPMOVE`. It doesn't create another.
- **Empty step alternatives** (two buttons):
  - Near move with 0 picks: "Go one step further" (the same move, further) plus the first available of "its artist", "this track", "its label", "a credited person" that isn't the empty move, taken from the same source pick.
  - Further move with 0 picks: the first two available of that list.
  - The source pick is gone (multi-origin): "Choose a different seed" (root) or nothing.

  The sentence names the two: `S-E-EMPTY`.
- **Needs Discogs:** label, artist and person moves read Discogs. Without a token, the menu items are disabled with `S-MENU-NOTOKEN` and the keys show `S-T-NOTOKEN-*`.

### 5.5 Return, history and reload

- **Ways back:** a trail row, U (parent), browser back and forward (Cmd+[ and Cmd+] are the browser's own shortcuts), a territory island, the map's from marker, also-in links, the kept list, G.
- **History entries:** each step change pushes one. Selection changes and the List/Map switch replace the current entry (D22). The new-dig screen is its own entry.
- **What returning restores:**
  - the snapshot's rows in rank order (with their facts as fetched) and its saved dot positions and hubs
  - the step's remembered selection and list scroll position
  - the route's view
  - the header meta reads `S-HDR-META-RETURN`
  - marks show the current judgments and heard state (C2)
- **U on the root step:** `S-T-ROOT`.
- **Reload restores:**
  - the route (dig, step, selection, view)
  - list scroll (the selected row scrolled into view)
  - trail path expanded
  - the territory camera on the open island
  - the player: queue, index and position, loaded paused with `S-LCD-RESTORED`. Playback waits for a press.
- **Reload mid-move** (D17):
  - If the server is still running the job, the step stays running and re-attaches to the SSE stream (events replay, rows don't duplicate).
  - If the server restarted, the step is `stopped` and shows the stopped block.

### 5.6 Re-run and retry

- **"Re-run"** (header H2, on done, failed-with-picks and stopped-with-picks steps) creates a new sibling step with the same parent, origins, move and further. It takes the next step number and opens at once. The old step is never edited.
- **The sibling shows the difference** (D16):
  - H1 tag "changed".
  - H6 `S-HDR-RERUN` (counts by track key against the original: same, new, gone). While streaming it reads `S-HDR-RERUN-RUN`, and "+ new" marks appear on rows as they arrive.
  - Rows new against the original carry "+ new". The "also in step {original}" mark is suppressed on the sibling's rows, because the header already counts them.
  - After the rows, the gone section lists the original's tracks the re-run lacks (§3.3).
  - The original's header shows `S-HDR-RERUN-LATER` (derived display; the snapshot is untouched).
  - On the map, new picks draw as normal; there's no extra glyph, because the list carries "new".
- **"Retry"** applies only to a failed or stopped step with 0 picks. It runs the same move again in the same step (status back to running), since there's no snapshot to protect. A failed or stopped step with 1 or more picks offers "Re-run" instead.
- **Rate limits:** after a cosine.club 429 or 5xx the button is disabled and counts down `S-E-RETRY-WAIT` (or `S-E-RERUN-WAIT`) from the server's Retry-After, or 30 s by default. At 0 it becomes `S-E-RETRY` / "Re-run". Nothing retries on its own.

### 5.7 Several moves at once

- Each step streams on its own, with its own "~ {i}/{n}" in the trail and progress in its header.
- A step finishing in the background never takes the view or the selection. Its trail marker turns into its count, and the announcer says `S-AN-STEP-DONE` (polite).
- A step failing in the background shows "! failed" in the trail and announces `S-AN-STEP-FAIL`. There's no toast and no view change.
- There's no client cap on running moves. If the server serialises them, the waiting step shows `S-HDR-WAITING`.
- The player and every other step keep working while moves run or fail.

### 5.8 Click, double-click and hover

| Element | Click | Double-click | Hover |
|---|---|---|---|
| Seed result | Add as chip | Same | Highlight |
| Chip × | Remove that chip | Nothing | — |
| "Dig from N tracks" | Start the dig | Nothing | — |
| Recent dig row | Open that dig | Nothing | `--panel-2` |
| Trail step | Open it (snapshot) | Nothing | `S-TR-TIP` |
| Trail stub | Expand that branch | Nothing | — |
| Kept-list row | Open its step, track selected | Nothing | — |
| Dig switcher item | Open that dig / new dig | Nothing | — |
| List row | Select | Play from this row | Buttons; its dot ringed |
| Row ▶ / ★ / ✕ | Play from this row / toggle keep / toggle pass | Nothing | — |
| Row "Sounds like ▾" | Open the move menu for this row | Nothing | — |
| Header "▶ Play step" / "Re-run" / "Retry" | P / re-run / retry | Nothing | — |
| Map dot | Select; list scrolls to its row | Play from this row | `S-MAP-TIP-PICK`; its row and hub lines highlight |
| Map hub | Select; its picks highlight | Nothing | `S-MAP-TIP-*`; its picks highlight |
| Map centre | Select the origin | Nothing | — |
| Map from marker | Open the parent step | Nothing | — |
| Empty map | Clear the selection | Fit | — |
| Territory island | Open that step | Nothing | `S-TR-TIP`; recurring hubs' other appearances highlight |
| Inspector fact name | Select that label or person | Nothing | — |
| Inspector "Sounds like it" / summary move buttons | Start that move | Nothing | — |
| Inspector also-in row | Open that step, track selected | Nothing | — |
| LCD title (L1) | Go to the playing track (G) | Nothing | — |
| LCD where-next button | Start that move | Nothing | — |
| LCD "Open in YouTube Music" | Open in a new tab | Nothing | — |
| Toast "Undo" | Undo | Nothing | Pauses the timer |
| Menu item / "» further" | Start that move, near / further | Nothing | Highlight |

---

## 6. Keys and focus

### 6.1 Key map

None of these fire while focus is in a text input, textarea or select, except where the last column says otherwise. On Feeds and Setup only `/`, `?`, G, `[`, `]`, K, X and Z work; everything else is native (D24).

| Key | Region | Action | In the seed box |
|---|---|---|---|
| `/` | Any page | Open the new-dig screen and focus the seed box; the player keeps playing | Types "/" |
| `?` | Any page | Open or close the key sheet | Types "?" |
| Up / Down | List, slim list | Move the selection one row | Moves the result highlight |
| Up / Down | Trail | Move focus between step and stub rows (doesn't open) | — |
| Up / Down | Map or territory canvas | Select the previous or next pick of the open step, in rank order | — |
| Up / Down | Menu, picker | Move the highlight | — |
| Tab / Shift+Tab | Everywhere | Next or previous focus stop (§6.3) | Native |
| Enter | List, map, territory with a selection | Open the move menu for it | Adds the highlighted result, or starts on an empty box with chips |
| Enter | Trail | Open the focused step, or expand the focused stub | — |
| Enter / Shift+Enter | Menu | Choose near / one step further | — |
| Backspace | Seed box, empty text | Drop the last chip | Native with text |
| T / L / A / C | Dig page | Sounds like this track / its label / its artist / a credited person (§5.4) | Ignored |
| Shift + T / L / A / C | Dig page | The same, one step further (about 30 s) | Ignored |
| M / Shift+M | Dig page, in a dig | Sounds like the keeps in this step / in the whole dig | Ignored |
| Space | List, map, territory, trail, page body | Play the selection from its row; on the playing track, pause or resume; with no selection: `S-T-SPACE-NOSEL` | Types a space |
| Space | A focused button or link | Native press | — |
| P / Shift+P | Dig page, in a dig | Play this step from the top, now / after the current track | Ignored |
| `[` / `]` | Any page | Previous / next track | Ignored |
| Left / Right | Dig page, outside menus | Seek back 15 s / forward 15 s | Moves the caret |
| Shift+Right | Dig page, outside menus | Seek forward 60 s | Native |
| K / X | Any page | Keep / pass on the playing track, or the selection when idle (X is built, D5) | Ignored |
| Z, Cmd+Z | Any page | Undo the last keep or pass | Cmd+Z is native text undo |
| G | Any page | Select the playing track and open its step | Ignored |
| U | Dig page, in a dig | Open the parent step | Ignored |
| Cmd+[ / Cmd+] | Any page | The browser's back and forward, which walk the steps you viewed | Browser's own |
| V | Dig page, in a dig | Switch List and Map | Ignored |
| Esc | Anywhere | Close the open menu, picker, switcher or sheet; otherwise collapse the expanded rail; otherwise clear the selection | Closes results, then clears text, then blurs |
| `+` (or `=`) / `-` | Map or territory canvas | Zoom in / out | — |
| `0` | Map or territory canvas | Fit | — |
| 1–9 | Picker | Choose that row | — |

Single-letter keys with Cmd, Ctrl or Alt held are left to the browser (Cmd+Z excepted). Keys pressed while the iframe has focus go to YouTube (§2.5).

### 6.2 Focus regions and tab order (Dig page, in a dig)

DOM order and tab order match. Each region shows a 3 px `--focus` outline on its focused element; canvases use an inset outline (offset −4 px).

1. Skip link `S-SKIP` (visible on focus) → jumps to the list.
2. Title strip: CRT toggle.
3. Faceplate: L1 (when loaded), transport (6 buttons), L3 buttons when shown, List, Map, Dig, Feeds, Setup. The iframe is skipped.
4. Trail: switcher button, tree (one stop, roving), kept-list toggle, kept rows when expanded.
5. Centre: H2 and H4 buttons and links, the list (one stop, roving), body buttons (empty-step moves, retry).
6. Right: map canvas (one stop; the zoom buttons are skipped), inspector buttons and links in reading order.
7. Key strip: toast "Undo" when shown, "? keys".

Tab wraps to the top. Opening the menu, picker, switcher or sheet moves focus inside it, and closing returns focus to the opener. The key strip's hints follow the region holding focus: trail, list, map, inspector, territory, menu, or search on the new-dig screen. Faceplate and key strip focus show the list hints.

### 6.3 Focus visibility

- The focus ring is never hidden by author content. Toasts live in the key strip (D27). The rail overlay closes on focus leave. Menus and sheets take focus themselves. Scroll containers keep the focused row in view (`scrollIntoView({block: "nearest"})`).
- Selection (dashed inside outline plus bar) and focus (solid outside ring) never share a shape.
- Every interactive target is at least 24 × 24 px. Buttons, rows, chips and menu items are at least 36 px tall (`--tap`). The chip × is at least 24 × 24.

### 6.4 The ? sheet

Title `S-KEYS-TITLE`. Six groups in two columns, each row a `kbd` plus the action text from §6.1:

| Group | Keys |
|---|---|
| Start | `/` · Enter (in the search box) · Backspace (empty search box) |
| Move around | Up/Down · Tab/Shift+Tab · Enter (trail) · U · G · Cmd+[ / Cmd+] · V · Esc |
| Play | Space · P / Shift+P · [ / ] · Left / Right · Shift+Right |
| Keep | K · X · Z / Cmd+Z |
| Dig from… | Enter · T L A C · Shift + T L A C · M / Shift+M |
| Map | + / − · 0 · drag to pan · double-click empty space to fit |

Footnotes: `S-KEYS-FOOT-1`, `S-KEYS-FOOT-2`, `S-KEYS-FOOT-3`.

---

## 7. Map grammar

### 7.1 Step map

- **Deterministic:** positions come only from the step's data, are saved with the step, and never move once placed. Actions animate only what changed (R19). Zoom and pan move the camera only. Opening a step fits the camera to the whole circle (D41).
- **Geometry:** circle radius R = min(width, height)/2 − 24 px.
  - Picks sit at distance r = clamp(0.08 + 0.77 × (1 − score) / (1 − floor), 0.08, 0.85) × R, where floor is the lowest score in the pick's batch: "radius = 1 − score" scaled to the batch (interaction-spec O14.2, superseding D3's fixed scale, which squeezed real multi-seed steps into the inner quarter).
  - Hubs sit on the outer ring at 0.95R.
- **Angle, one origin:** a stable, well-mixed hash of the saved pick id, mapped to 0–360° (interaction-spec §9 and O14.1).
- **Angle, several seeds:** one equal sector per seed, in chip order clockwise from 12 o'clock. A pick sits in the sector of the seed it's closest to (argmax per-seed score), at a hashed angle inside the middle two-thirds of that sector. A pick close to 2 or more seeds sits on the boundary between its top two seeds' sectors, within ±1/6 sector of hashed jitter. Its higher fused score already puts it nearer the centre.
- **Hubs:** only a label or credited person that is a Discogs fact on 2 or more of the step's picks. Never a move's `via`, never inferred. Person hubs count credited people other than the track's own artist (D43).
  - A hub sits at the circular mean angle of its picks; collisions step it along the ring by 8° until clear, in a fixed order (hub kind, then name).
  - Hubs appear as facts land, top-ranked picks first; nothing else waits for them.
- **No hubs:** no decorative ring. The caption `S-MAP-NOHUB` shows (with facts progress while loading; wording per D2).
- **Selected pick label:** the selected pick shows "Artist - Title" (28 characters max) beside it; other titles show only in the hover tooltip, clamped inside the map panel. Label collision code from `map.js` is reused.
- **Hit targets:** pick 12 px radius, hub and origin 14 px.
- **Colours** are read from CSS at runtime (`getComputedStyle(document.documentElement)`), never hard-coded.

### 7.2 Territory

- **Islands** = steps. The root sits at (0, 0). Each child is placed 420 world units from its parent, in the direction pointing away from the grandparent (root's children start pointing east). Siblings fan out at 0°, +30°, −30°, +60°, −60°… in creation order. If a spot overlaps an existing island, the distance grows by 140 until clear. The layout is deterministic.
- **Open island:** the route's step, drawn in full. **Collapsed islands:** §4.6.
- **Lines:**
  - Branch: a dotted line from the node branched from (the pick, hub or origin position inside the parent island) to the child's centre, `--muted` at 60% alpha.
  - Recurring hub: 1 px solid in the hub's colour at 25% alpha, joining a hub's appearances in step order.
- **Selecting a pick:** dashed square on it, and the branch lines from step 1 to its step brighten to full alpha in `--text`.
- **Selecting a hub:** its appearances in other islands get rings and their lines brighten.
- **Camera:** when the open island changes, the camera pans and zooms over 300 ms (ease-out) to fit it; with reduced motion it jumps at once. `0`, Fit or a double-click on empty canvas fits the whole dig.
- At 40 steps the dig stays readable because collapsed islands draw only origins, keeps and recurring hubs.

### 7.3 Motion

| Motion | Normal | `prefers-reduced-motion: reduce` |
|---|---|---|
| Playing ring | Pulse, 1.4 s loop | Static double ring |
| Listening ring | Expanding, 1.6 s loop | Static dashed ring |
| Dot and hub arrival | 200 ms fade | Instant |
| Territory camera | 300 ms pan | Instant |
| Spectrum | Moving bars while playing | Static bars while playing |
| Rail expand | 120 ms width | Instant |
| Scroll to row | Smooth | Jump |
| Row arrival | 200 ms fade | Instant |
| CRT roll | Existing | None (existing rule) |
| Trail "~" | Static glyph | Static glyph |
| Toast | Appears, no slide | Same |

---

## 8. String catalog

Exact wording. Sentence case in the source; uppercase only via CSS where §3 says so. `{…}` are runtime values. Numbers use numerals. Times are 24-hour local time ("15:07"); dates read "Thu 2 Oct".

### 8.1 Faceplate and shell

| Id | Text |
|---|---|
| S-LCD-IDLE | nothing playing yet |
| S-LCD-IDLE-HINT | Space plays the selected track · P plays this step |
| S-LCD-IDLE-HINT-NOSEL | ▶ PLAY STEP or P plays this step · ↓ selects a track |
| S-LCD-FROM | from step {n}: {sentence with a lowercase first letter} |
| S-LCD-FROM-DIG | from {dig name}, step {n}: {sentence with a lowercase first letter} |
| S-LCD-REFUSED | Can't play here: the owner turned off embedding. Skipping in {s} s |
| S-LCD-UNAVAIL | Can't play here: this video isn't available. Skipping in {s} s |
| S-LCD-NOVIDEO | No video for this track. Skipping in {s} s |
| S-LCD-OFFLINE | The player can't reach YouTube. Track links still work. |
| S-LCD-RESTORED | Paused after reload. Press ▶ to resume. |
| S-LCD-GESTURE | Press ▶ to start playback. |
| S-LCD-WAITING | Waiting for more tracks from step {n}… |
| S-LCD-UPNEXT | Up next: step {n} from the top |
| S-LCD-END | Step {n} finished. Where next: |
| S-LCD-WN-LABEL-ARIA | Sounds like the label {Label} |
| S-LCD-WN-PERSON-ARIA | Sounds like records {Person} is credited on |
| S-LCD-WN-TRACK-ARIA | Sounds like {Artist - Title} |
| S-LCD-WN-ARTIST | its artist: {Artist} |
| S-VID-NOSIGNAL | NO SIGNAL |
| S-LISTMAP-OFF | Open a dig to switch views |
| S-NARROW | cosine-digger is built for desktop windows 1100 px and wider |
| S-SRV-LED-OK | Server: connected |
| S-SRV-LED-DOWN | Server: lost |
| S-SRV-LOST | Lost the local server. Your dig is saved; reconnecting… |
| S-SRV-LOST-START | Can't reach Digger at this address. Your digs are saved. In Terminal, cosine-digger start starts it again, or opens it where it's already running. |
| S-SRV-LOST-SERVE | Can't reach Digger at this address. Your digs are saved. Start Digger again the way you started it; this page reconnects when it's back. |
| S-SKIP | Skip to the step list |

### 8.2 Seed box and new dig

| Id | Text |
|---|---|
| S-SB-TITLE | Start a dig |
| S-SB-ASIDE | add one seed or several |
| S-SB-LEDE | Start from a track you love. |
| S-SB-PH-0 | Type an artist or a track, or paste a link |
| S-SB-PH-N | Add another seed, or press Enter to start |
| S-SB-SEARCHING | ~ Searching cosine.club… |
| S-SB-SEC-COSINE | On cosine.club |
| S-SB-SEC-LIB | In your library |
| S-SB-SEC-LIB-ASIDE | (any scanned folder or importer) |
| S-SB-LIB-IDX | your file · on cosine.club |
| S-SB-LIB-NOTIDX | your file · not on cosine.club, so the dig would start from indexed tracks by {Artist} and say so |
| S-SB-LIB-IDX-IMP / -NOTIDX-IMP / -IDX-UNKNOWN-IMP | in your {Source} library · … (the same endings, for imported rows such as Plex) |
| S-SB-MORE | … more matches |
| S-SB-NOMATCH | No match on cosine.club for '{query}'. Try artist and title, or paste a link. |
| S-SB-LIB-NOMATCH | No match in your library. |
| S-SB-NOLIB | Scan a music folder in Setup to start from your own files |
| S-SB-NOKEY | Add your cosine.club key to search. |
| S-SB-KEY-REJECTED | cosine.club didn't accept your key. Fix it to search and dig. |
| S-SB-UNREACHABLE | Couldn't reach cosine.club. Check your connection. |
| S-SB-LIB-STILL | Library matches still work. |
| S-SB-BUSY | cosine.club is busy. Try again in {s} s. |
| S-SB-BUSY-NOW | cosine.club is busy. |
| S-SB-SERVER-ERR | cosine.club didn't answer: server error. Try again in {s} s. |
| S-SB-SERVER-ERR-NOW | cosine.club didn't answer: server error. |
| S-SB-DIGGER-ERR | Digger's search failed: {reason}. |
| S-SB-DIG-NOKEY | Digging needs your cosine.club key. |
| S-SB-LINK-ROW | Use this link: {host/path, 48 characters max} |
| S-SB-LINKS | or paste a Discogs, YouTube or cosine.club link |
| S-SB-ADDED | added |
| S-SB-DUP | Already a seed. |
| S-SB-MAX | Up to 10 seeds per dig. |
| S-SB-ENTER-WAIT | Still searching. Press Enter again when a match appears. |
| S-SB-EMPTY-ENTER | Type an artist or a track first. |
| S-SB-REMOVED | Removed {chip text} |
| S-SB-CHIP-X | Remove {chip text} |
| S-SB-CHIP-NOTIDX | Not on cosine.club: the dig will use indexed tracks by {Artist} |
| S-SB-BTN | Dig from {n} track ▸ / Dig from {n} tracks ▸; when any chip isn't a track: Dig from {n} seed ▸ / Dig from {n} seeds ▸ |
| S-SB-BTN-HINT | or Enter on an empty box |
| S-SB-STARTING | Starting… |
| S-SB-START-ERR | Couldn't start the dig: {reason}. Your seeds are still here. |
| S-SB-RECENT | Recent digs |
| S-SB-RECENT-META | {when} · {s} steps · {k} kept (omit "· {k} kept" at 0; "1 step") |
| S-SB-DAILY | Today's Daily |
| S-SB-DAILY-META | {n} tracks · {Ddd D Mon} |
| S-SB-DAILY-FEEDS | See it in Feeds |
| S-KEY-TITLE | Connect cosine.club |
| S-KEY-LEDE | Digger finds similar tracks through cosine.club. Paste your API key to start digging. |
| S-KEY-LABEL | cosine.club API key |
| S-KEY-PH | Paste it here |
| S-KEY-HELP | No key yet? Make a free account at cosine.club, then copy the key from your account page ↗ |
| S-KEY-HELP-LINK | your account page ↗ |
| S-KEY-BTN | Connect and start digging |
| S-KEY-CHECKING | Checking… |
| S-KEY-PRIVACY | Digger keeps the key on this computer and sends it only to cosine.club. |
| S-KEY-EMPTY | Paste your key first. |
| S-KEY-MALFORMED | That key has a space or line break in it. Copy it again and paste it here. |
| S-KEY-MALFORMED-ENV | That key has a space or line break in it. |
| S-KEY-REJECTED | cosine.club didn't accept that key. Copy it again from your account page. |
| S-KEY-UNREACHABLE | Couldn't reach cosine.club to check the key. Check your connection, then try again. |
| S-KEY-BUSY | cosine.club asked Digger to wait {n} seconds. Try again then. |
| S-KEY-SERVER-ERR | cosine.club had a server error while checking the key. Try again in {n} seconds. |
| S-KEY-ENV | Using the key from {variable} (ends …{last4}), set where Digger was started. To change it, run cosine-digger stop, change that variable, then run cosine-digger start. |
| S-KEY-ENV-EMPTY | {variable} is set but empty where Digger was started. To use a key, run cosine-digger stop, set that variable to your key or remove it, then run cosine-digger start. |
| S-KEY-OK | Connected to cosine.club. |
| S-KEY-SAVE-FAILED | The key works, but Digger couldn't save it: {reason}. Try again. |
| S-KEY-DIGGER-ERR | Digger couldn't check the key: {reason}. Try again. |
| S-KEY-SUPERSEDED | Another key was saved while this one was checked. |
| S-KEY-NOTICE | Add your cosine.club key to search and start new digs. Your saved digs still open and play. |
| S-KEY-ADD | Add key |
| S-KEY-FIX | Fix key |
| S-KEY-TRY-AGAIN | Try again |

### 8.3 Step header, notes and bodies

Label moves always read "sounds like" (D1). The phrase "released on" never appears anywhere in the product.

| Id | Text |
|---|---|
| S-HDR-SEED-1 | Sounds like {Artist - Title} |
| S-HDR-SEED-N | Sounds like {n} seeds: {s1}, {s2}, {s3} (4 or more: "{s1}, {s2}, {s3} and {k} more") |
| S-HDR-TRACK | Sounds like {Artist - Title} |
| S-HDR-LABEL | Sounds like the label {Label} |
| S-HDR-ARTIST | Sounds like the artist {Artist} |
| S-HDR-PERSON | Sounds like records {Person} is credited on |
| S-HDR-THESE-STEP | Sounds like your {k} keeps in step {n} |
| S-HDR-THESE-DIG | Sounds like your {k} keeps in this dig |
| S-HDR-FURTHER | (suffix) , one step further |
| S-HDR-META | {HH:MM} · {c} tracks · {k} kept · {p} passed (omit zero parts; date first when not today: "Thu 2 Oct 15:07") |
| S-HDR-META-RETURN | {HH:MM}, as you left it · {c} tracks · {k} kept · {p} passed |
| S-HDR-FROM-TRACK | from step {p}: {Artist - Title} |
| S-HDR-FROM-LABEL | from step {p}: the label of {Artist - Title} |
| S-HDR-FROM-ARTIST | from step {p}: the artist of {Artist - Title} |
| S-HDR-FROM-PERSON | from step {p}: {role} on {Artist - Title} |
| S-HDR-FROM-HUB-LABEL | from step {p}: a label {k} picks share |
| S-HDR-FROM-HUB-PERSON | from step {p}: a person credited on {k} picks |
| S-HDR-FROM-THESE | from step {p}: {k} keeps: {t1}, {t2}, {t3} (and {m} more) |
| S-HDR-PROGRESS | listening for neighbours · {i} of {n} (further, after the first batch: "going one step further · {i} so far") |
| S-HDR-HIDDEN | Hidden from this step: {parts}. Parts in this order, zero parts omitted: "{n} you own", "{n} already in your Daily or Weekly", "{n} you kept", "{n} you passed", "{n} by artists or labels you muted", "{n} you've dug from before". An info line in `--muted` under the header, not a warning. It counts only tracks that would otherwise have made the step. |
| S-HDR-WAITING | waiting for another move to finish |
| S-HDR-RESOLVING | reading Discogs releases for {name}… |
| S-HDR-TAG-RUNNING | ~ running |
| S-HDR-TAG-WAITING | ~ waiting |
| S-HDR-TAG-FAILED | ! failed |
| S-HDR-TAG-STOPPED | ‖ stopped |
| S-HDR-TAG-CHANGED | changed |
| S-HDR-RERUN | + Re-run of step {n} from {HH:MM}: {a} the same, {b} new, {c} gone. |
| S-HDR-RERUN-RUN | + Re-run of step {n} from {HH:MM} · comparing as tracks arrive |
| S-HDR-RERUN-LATER | Re-run later as step {m} |
| S-N-SWAP-FILE | Your file '{seed}' isn't on cosine.club. Using {n} indexed {Artist} tracks instead: {t1}, {t2}. (1: "Using 1 indexed {Artist} track instead: {t1}."; more than 3: "{t1}, {t2}, {t3} and {k} more.") Button: Choose a different seed |
| S-N-SWAP-TEXT | '{seed}' isn't on cosine.club. Using {n} indexed {Artist} tracks instead: {titles}. |
| S-N-CLOSEST | '{seed}' matched '{Artist - Title}', the closest version on cosine.club. |
| S-N-DROPPED | '{seed}' isn't on cosine.club and has no stand-in, so this step uses your other seeds. |
| S-N-PARTIAL | {k} of your {n} seeds are on cosine.club. (followed by the swap or dropped notes) |
| S-N-NONE | None of your seeds are on cosine.club, and it has no tracks by {Artist(s)} yet. Try another track by them, or paste a cosine.club link. Button: Choose a different seed |
| S-N-DUP | '{Artist - Title}' is in more than one of your seeds; it counts once. |
| S-N-ALBUM | The album '{Album}' has {t} tracks; {u} of them are used so every seed gets a turn (10 seed tracks at most). |
| S-N-KEEPS-CAP | Using your 10 most recent keeps of {k}. |
| S-N-LABEL-FEW | Only {m} of the newest releases on {Label} are on cosine.club, so this step leans on them. |
| S-N-FACTS-BUDGET | This step used its Discogs lookups, so some picks have no facts. |
| S-N-FACTS-DOWN | Discogs isn't answering, so some picks have no facts. |
| S-N-FACTS-STOPPED | The step stopped before every pick had facts. |
| S-E-EMPTY | Nothing came back for this move. Try {alt 1}, or {alt 2}. (alts: "sounds like this track", "sounds like its label", "sounds like its artist", "sounds like a credited person", "go one step further"; default: "Nothing came back for this move. Try sounds like its artist, or go one step further.") |
| S-E-EMPTY-BTN-* | Sounds like this track · Sounds like its label · Sounds like its artist · Sounds like a credited person · Go one step further · Choose a different seed |
| S-E-FAIL-COSINE | cosine.club didn't answer: {rate limited \| server error \| timed out \| unreachable}. Nothing was lost. |
| S-E-FAIL-DISCOGS | Discogs didn't answer, so {the label's releases \| the artist's records \| their credits} couldn't be read: {reason}. Nothing was lost. |
| S-E-FAIL-OTHER | This move stopped with an error: {plain message, secrets redacted}. Nothing was lost. |
| S-E-FAIL-L2 | Your other steps and the player keep working. |
| S-E-RETRY | Retry |
| S-E-RETRY-WAIT | Retry in {s} s |
| S-E-RERUN-WAIT | Re-run in {s} s |
| S-E-STOPPED | This move stopped when the app closed. {i} of {n} tracks had arrived; they're kept. (0: "This move stopped when the app closed before any tracks arrived.") |
| S-E-NOTOKEN-L | Sounds like its label needs a Discogs token. Add one in Setup. |
| S-E-NOTOKEN-A | Sounds like its artist needs a Discogs token. Add one in Setup. |
| S-E-NOTOKEN-C | Sounds like a credited person needs a Discogs token. Add one in Setup. |
| S-E-KEY-PARTIAL | cosine.club stopped accepting your key, so this step stopped early. {i} of {n} tracks had arrived; they're kept. |
| S-E-CONNECT-DISCOGS | Connect Discogs |

### 8.4 Rows

| Id | Text |
|---|---|
| S-ROW-TITLE | {Artist - Title} · Digger score {0.79} |
| S-ROW-REFUSED | ! can't play here |
| S-ROW-UNAVAIL | ! isn't available |
| S-ROW-NOVIDEO | no video |
| S-ROW-KEPT | ★ kept |
| S-ROW-KEPT-IN | ★ kept in step {n} |
| S-ROW-PASSED | ✕ passed |
| S-ROW-PASSED-IN | ✕ passed in step {n} |
| S-ROW-NEW | + new |
| S-ROW-HEARD | heard |
| S-ROW-HEARD-IN | heard in step {n} |
| S-ROW-ALSO | also in step {n} (several: "also in steps {n}, {m}") |
| S-ROW-ALSOON | ■ also on {Label} |
| S-ROW-WHY-ALL | close to all {n} seeds |
| S-ROW-WHY-K | close to {k} of your {n} seeds |
| S-ROW-WHY-1 | close to {seed Artist - Title} |
| S-ROW-WHY-FURTHER | one step further: sounds like {the label {X} \| the artist {X} \| records {X} is credited on} |
| S-ROW-DIVIDER | One step further · sounds like the labels, artists and people of the picks above |
| S-ROW-DIVIDER-RUN | ~ {i} so far |
| S-ROW-GONE-DIVIDER | Not in this re-run · {c} tracks step {n} had |
| S-ROW-BTN | ▶ Play · ★ Keep / ★ Kept · ✕ Pass / ✕ Passed · Dig from ▾ |

### 8.5 Trail

| Id | Text |
|---|---|
| S-TR-META | {s} steps · {k} kept ("1 step"; omit kept at 0) |
| S-TR-SWITCH | Switch dig |
| S-TR-NEW | + New dig |
| S-TR-SWITCH-ROW | {dig name} · {when} · {s} steps · {k} kept ("1 step"; omit kept at 0) |
| S-TR-ROOT-1 | sounds like it |
| S-TR-ROOT-N | sounds like {n} seeds |
| S-TR-THESE | {k} keeps |
| S-TR-STUB | + {n} more steps (with keeps: "+ {n} more steps · ★{k}"; 1: "+ 1 more step") |
| S-TR-TIP | {Sentence} · {c} tracks · {k} kept · {p} passed · {HH:MM} |
| S-TR-KEPT-H | Kept in this dig ({k}) |
| S-TR-KEPT-ROW | {Artist - Title} · step {n} |
| S-TR-KEPT-EMPTY | Press K while a track plays to keep it |
| S-TR-LEGEND | ▶ holds the playing track · ~ running |

### 8.6 Map and territory

| Id | Text |
|---|---|
| S-MAP-TITLE | Map of step {n} |
| S-MAP-LEGEND | nearer = more alike · ■ label ◯ person shared by 2+ picks |
| S-MAP-NOHUB | No shared labels or people yet (while facts load: "No shared labels or people yet · facts {i} of {n}") |
| S-MAP-DISCOGS-DOWN | ! Discogs isn't answering, so shared labels and people are missing. |
| S-MAP-FACTS-PARTIAL | ! Some picks have no facts, so shared labels and people may be incomplete. |
| S-MAP-FROM | ◁ from step {p} |
| S-MAP-TIP-PICK | {Artist - Title} · Digger score {0.79} (when the fact is known: " · on {Label}") |
| S-MAP-TIP-LABEL | {Label} · on {k} picks here (when it recurs: " · also in step {n}") |
| S-MAP-TIP-PERSON | {Person} · credited on {k} picks here ({roles}) (when it recurs: " · also in step {n}") |
| S-TERR-TITLE | The whole dig, drawn as a map |
| S-TERR-LIST-H | Step {n} · {c} tracks |

### 8.7 Inspector

| Id | Text |
|---|---|
| S-INS-K-TRACK / -LABEL / -PERSON / -ORIGIN / -STEP | Track / Label / Person / Origin / Step {n} |
| S-INS-WHY-H | Why it's here |
| S-INS-WHY-1 | {Sentence}. Rank {r} of {c}. |
| S-INS-WHY-N | Close to {k} of your {n} seeds, by cosine.club similarity: {seed} {0.81}, {seed} {0.77}. Rank {r} of {c}. (all: "Close to all {n} seeds, by cosine.club similarity: …"; one: "Close to {seed}: cosine.club similarity {0.81}.") |
| S-INS-WHY-FURTHER | One step further: sounds like {the label {X} \| the artist {X} \| records {X} is credited on}, which is on {k} picks above. |
| S-INS-SCORE | Digger score {0.79}: Digger's own order, merged from each origin's cosine.club list. The strongest candidate scores 1.00, even if the step hid it. (Shown under every pick's why line, decided 3 Oct 2026: the score is Digger's ranking, not cosine.club's.) |
| S-INS-SCORE-FURTHER | Digger score {0.79}: Digger's own order for the picks one step further, merged from the cosine.club lists it followed. Their strongest candidate scores 1.00, even if the step hid it. |
| S-INS-SIMS-H / -FURTHER-H / -BRANCH-H | cosine.club similarity to each seed / cosine.club similarity through the picks above / cosine.club similarity to the branch it followed |
| S-INS-ALSO-H | Also in |
| S-INS-ALSO-ROW | Step {n} · {sentence} · rank {r} |
| S-INS-HEARD | Heard in step {n} at {HH:MM} |
| S-INS-FACTS-H | Facts |
| S-INS-FACTS-ASIDE | from Discogs |
| S-INS-FACTS-LOADING | ~ Loading facts from Discogs ({i} of {n} done for this step) |
| S-INS-FACTS-MISSING | Discogs has no release for this track. |
| S-INS-FACTS-DOWN | ! Facts unavailable: Discogs isn't answering. |
| S-INS-FACTS-BUDGET | Facts not fetched: this step used its Discogs lookups. |
| S-INS-FACTS-STOPPED | Facts not fetched: the step stopped first. |
| S-INS-FACTS-NOTOKEN | Label, year and credits come from Discogs (not connected). |
| S-INS-ALSO-ON | Also on {Label}: this track really is on the label this step sounds like. |
| S-INS-CREDITS-MORE | + {k} more credits |
| S-INS-DISCOGS | Discogs release ↗ |
| S-INS-SOUNDS | Dig from ▸ |
| S-INS-SOUNDS-FAR | Dig one step further ▸ |
| S-INS-HUB-LABEL | {k} picks here are on this label. |
| S-INS-HUB-PERSON | Credited on {k} picks here: {roles}. |
| S-INS-HUB-ALSO | Also in step {n} ({k} picks) |
| S-INS-ORIGIN-H | Where step {n} started |
| S-INS-ORIGIN-SEED | {Artist - Title} · {k} picks sit closest to it |
| S-INS-ORIGIN-FROM | from step {p} |
| S-INS-SUM-COUNTS | {c} tracks · {k} kept · {p} passed · {h} heard |
| S-INS-SUM-HUBS | Shared here: ■ {Label} ({k}) · ◯ {Person} ({k}) |
| S-INS-SUM-FACTS | Facts {i} of {n} |
| S-INS-SUM-M | Sounds like the keeps in this step ({k}) |
| S-INS-SUM-SHIFTM | Sounds like the keeps in this dig ({k}) |
| S-INS-SUM-KEEPS | Sounds like your keeps ({k}) |
| S-INS-SUM-HINT | Click a track to see why Digger picked it. |

### 8.8 Menu, pickers, keys

| Id | Text |
|---|---|
| S-MENU-TITLE | Next step: tracks that sound like… |
| S-MENU-FROM | from: {Artist - Title} (hub: "from: the label {Label}" / "from: {Person}") |
| S-MENU-T | this track · about 3 s |
| S-MENU-L | its label: {Label} · about 10 to 30 s (several: "its label ▸") |
| S-MENU-A | its artist: {Artist} · about 4 to 8 s (several: "its artist ▸") |
| S-MENU-C | a credited person ▸ · about 10 to 30 s |
| S-MENU-HUB-L | the label {Label} · about 10 to 30 s |
| S-MENU-HUB-C | records {Person} is credited on · about 10 to 30 s |
| S-MENU-FOOT | Shift + key: go one step further, slower (about 30 s) |
| S-MENU-FAR-ARIA | {item}, one step further |
| S-MENU-LOADING | (facts loading) |
| S-MENU-NOLABEL | (none listed) |
| S-MENU-NONE | (none listed yet) while facts load, then (none listed) |
| S-MENU-NOTOKEN | (needs a Discogs token) |
| S-PICK-LABEL | Which label? |
| S-PICK-ARTIST | Which artist? |
| S-PICK-PERSON | Which credited person? |
| S-KEYS-TITLE | Keys |
| S-KEYS-FOOT-1 | K and X judge the playing track; with nothing playing, the selected one. |
| S-KEYS-FOOT-2 | Letter keys don't work while you type in the search box. |
| S-KEYS-FOOT-3 | If you click into the video, YouTube's own keys apply until you click outside it. |

### 8.9 Key strip

| Id | Text |
|---|---|
| S-KS-SEARCH | Enter adds a seed · Enter on an empty box starts · Backspace drops last chip · ? keys |
| S-KS-LIST | ↑↓ select · Space play · K keep the playing track · T L A C dig from… · ? keys |
| S-KS-TRAIL | ↑↓ move · Enter open step · U parent step · Cmd+[ back · ? keys |
| S-KS-MAP | ↑↓ select · Enter dig from… · + − zoom, 0 fit · V whole dig · ? keys |
| S-KS-INSPECTOR | Tab next button · Enter press it · T L A C dig from… · ? keys |
| S-KS-TERR | V list view · click a step to open it · L sounds like this label · ? all keys |
| S-KS-MENU | T L A C choose · Shift goes one step further · Esc close · ? keys |
| S-KS-IFRAME | Keys go to YouTube while the video has focus. Click outside the video to get them back. |

("K keep the playing track" becomes "K keep the selected track" when idle, per §2.5.)

### 8.10 Toasts and announcements

| Id | Text |
|---|---|
| S-T-KEPT | Kept {Artist - Title}. Daily and Weekly look for more like your keeps. Z undoes it. (button: Undo) |
| S-T-KEPT-WAS | Kept {Artist - Title} (it was passed). Z undoes it. |
| S-T-PASSED | Passed {Artist - Title}. It stays out of your Daily and Weekly. Z undoes it. (button: Undo) |
| S-T-ALREADY-KEPT | Already kept. |
| S-T-ALREADY-PASSED | Already passed. |
| S-T-UNDO-KEEP | Undid the keep on {Artist - Title}. |
| S-T-UNDO-PASS | Undid the pass on {Artist - Title}. |
| S-T-NOUNDO | Nothing to undo. |
| S-T-NOTARGET-K | Play or select a track to keep it. |
| S-T-NOTARGET-X | Play or select a track to pass it. |
| S-T-NOSEL | Select a track first, or press G to select the playing track. |
| S-T-SPACE-NOSEL | Select a track first, or press P to play this step. |
| S-T-NOTHINGPLAYING | Nothing is playing. |
| S-T-FACTS-LOADING | Its label isn't known yet: facts are still loading. |
| S-T-FACTS-LOADING-C | Its credits aren't known yet: facts are still loading. |
| S-T-NOLABEL | Discogs lists no label for this track. |
| S-T-NOPERSON | Discogs lists no credited people for this track. |
| S-T-NOTOKEN-L / -A / -C | = S-E-NOTOKEN-L / -A / -C |
| S-T-WRONGHUB-PERSON | That's a person. Press C for records they're credited on. |
| S-T-WRONGHUB-LABEL | That's a label. Press L to sound like it. |
| S-T-WRONGHUB-LABEL-T, -A | That's a label. Press L to sound like it. |
| S-T-WRONGHUB-PERSON-T, -A | That's a person. Press C for records they're credited on. |
| S-T-NOKEEPS-STEP | Nothing kept in this step yet. Press K while a track plays to keep it. |
| S-T-NOKEEPS-DIG | Nothing kept in this dig yet. Press K while a track plays to keep it. |
| S-T-DUPMOVE | You already made this move: step {n}. |
| S-T-SAMEMOVE | That's this step's move. Use Re-run to run it again. |
| S-T-ROOT | Step 1 is where this dig starts. |
| S-T-MOVE-ERR | Couldn't start the move: {reason}. |
| S-T-JUDGE-ERR | Couldn't save that {keep \| pass}: {reason}. It's undone; try again. |
| S-T-RECONNECTED | Reconnected to the local server. |
| S-AN-STEP-START | Step {n} started: {sentence} |
| S-AN-STEP-DONE | Step {n} finished: {c} tracks |
| S-AN-STEP-FAIL | Step {n} failed: {reason} |
| S-AN-PLAYING | Now playing {Artist - Title}, from step {n} |

### 8.11 Feeds, Setup, About

| Id | Text |
|---|---|
| S-FEEDS-TITLE | Feeds |
| S-FEEDS-LEDE | Digger writes two private YouTube Music playlists from your keeps, your passes and your library, and refreshes them on schedule. Looking around in a dig never changes them. Nothing you own or were already served, ever. |
| S-FEEDS-CLOCK | LOCAL TIME {HH:MM} |
| S-FEEDS-NEXT-DAILY | NEXT DAILY {DDD HH:MM} |
| S-FEEDS-NEXT-WEEKLY | NEXT WEEKLY {DDD HH:MM} |
| S-SET-LIB-HELP | Any folder of tagged audio files works; Plex is optional. Scanning only reads your files and never changes their tags. Tracks you own never come back as picks or land in a playlist. |
| S-SET-LIB-SUM | Your library: {n} tracks {from}. ("1 track"; from = "from your music folder" or "from {Source}, imported {29 Sep}") Shown at the top of the Library panel. |
| S-SET-LIB-SUM-MIX | Your library: {n} tracks ({k} from {Source}, imported {29 Sep}; {k} from your music folder). |
| S-SET-LIB-SUM-EMPTY | Your library is empty. Scan a music folder below to add tracks. |
| S-SET-FILL-LABEL | Missing labels |
| S-SET-FILL-BTN | Fill missing labels from Discogs |
| S-SET-FILL-IDLE | {n} of {m} tracks in your library have no label. |
| S-SET-FILL-NONE | Every track in your library has a label. |
| S-SET-FILL-RUN | Looking up labels on Discogs: {i} of {n}, about {t} left. |
| S-SET-FILL-DONE | ✓ Filled {k} labels. Discogs had no label for {u} tracks. |
| S-SET-FILL-STOPPED | Stopped after {i} of {n}; {k} labels filled. The next run picks up here. |
| S-SET-FILL-ERR | ! Discogs isn't answering. {k} labels filled so far; try again later. |
| S-SET-FILL-NOTOKEN | Needs a Discogs token (Discogs, above), so this is skipped. |
| S-SET-FILL-NOLIB | Scan a folder first. |
| S-SET-FILL-BUDGET | Uses up to {b} Discogs lookups per run, about 1 per second. You can stop at any time. |
| S-ABOUT-TITLE | About |
| S-ABOUT-START | Digger runs as a small server on this computer. It keeps running when you close this tab or Terminal, and stops when you run cosine-digger stop or restart the computer. To open it again later, run cosine-digger start; cosine-digger status shows whether it's running and its address. Your settings are in {data folder} and your digs and keeps in {database file}. |
| S-ABOUT-SERVE | Digger is running where you started cosine-digger serve, in a Terminal window or a container, and stops when that stops. Your settings are in {data folder} and your digs and keeps in {database file}. |
| S-ABOUT-RANK | How Digger ranks: each step asks cosine.club for the tracks nearest each origin, merges those lists by rank so picks near several origins rise, and hides tracks you own, kept, passed, muted or were already served. The Digger score is that merged order, scaled so the strongest candidate scores 1.00, even if the step hid it; picks one step further are scaled on their own. Similarity numbers are cosine.club's own. |
| S-ABOUT-2 | This app embeds YouTube's player to play tracks. YouTube Terms of Service · Google Privacy Policy |
| S-ABOUT-3 | If your browser blocks YouTube's cookies, YouTube can't see your Premium account, so ads may play. |

---

## 9. Edge cases

Every edge case in the handoff, with its UI outcome.

| Case | Outcome |
|---|---|
| Seed not on cosine.club | Root step header H4: `S-N-SWAP-FILE` (library file) or `S-N-SWAP-TEXT` (typed or linked) naming the indexed tracks used, with "Choose a different seed" (opens `#/dig/new` with this dig's seeds as chips, minus that one). Several seeds: `S-N-PARTIAL` first. Nothing resolves: `S-N-NONE`, step done with 0 picks, no stack trace |
| Mixed and duplicate seeds | Chips of any kind together. A duplicate chip is refused (`S-SB-DUP`). Server-side overlap (an album containing a track chip) collapses with `S-N-DUP`. A truncated album gets `S-N-ALBUM` |
| A seed comes back as a pick | Excluded, as today; nothing shown |
| Kept or passed tracks | Excluded from later moves; never reappear as picks. The kept list shows "{track} · step {n}" |
| Same track in several steps | Rows show `S-ROW-ALSO` and heard marks; the inspector's "Also in" lists each step with its sentence and rank |
| Embed refused (101, 150) | Row "!" + `S-ROW-REFUSED`; L3 `S-LCD-REFUSED` + "Open in YouTube Music"; auto-skip at 2 s |
| Removed or private (100) | The same with `S-ROW-UNAVAIL` and `S-LCD-UNAVAIL` |
| First play needs a gesture | Every play starts from a key or click. If blocked: `S-LCD-GESTURE`. After reload: `S-LCD-RESTORED`, no autoplay |
| Player can't reach YouTube | L3 `S-LCD-OFFLINE`; frame `S-VID-NOSIGNAL`; links still work |
| YouTube cookies blocked | Mentioned once: `S-ABOUT-3` in Setup |
| Clicking into the video | YouTube's keys take over; key strip `S-KS-IFRAME`; the iframe isn't in the tab order |
| cosine.club 429 or 5xx | Step failed: `S-E-FAIL-COSINE` + `S-E-FAIL-L2` + countdown button; other steps and the player keep working |
| Discogs 429, slow or missing | Facts show `S-INS-FACTS-LOADING` or `S-INS-FACTS-DOWN`; map `S-MAP-DISCOGS-DOWN`; hubs missing; nothing else waits |
| Several moves at once | Each streams on its own; trail "~" per step; a background finish never takes the view (§5.7) |
| Reload or quit mid-move | Server alive: re-attaches, still running. Server restarted: "‖ stopped" with Retry (0 picks) or Re-run. Route, selection and queue position restored; playback waits |
| Re-run | A sibling marked "changed" with `S-HDR-RERUN`, "+ new" marks and the gone section; the original untouched, showing `S-HDR-RERUN-LATER` |
| A step with no picks | `S-E-EMPTY` + two move buttons (§5.4) |
| Library without label tags | Setup "Missing labels" job, budgeted, resumable, with progress; without a Discogs token `S-SET-FILL-NOTOKEN` |
| Very long titles | Ellipsis in rows, trail, chips, slim list and LCD; full text in `title` tooltips and the inspector |
| Keys while typing | Single-letter and symbol keys are ignored in the seed box and every other text input (§6.1) |
| No cosine.club key | With no saved digs, Start a dig is the key screen (`#connect`, `S-KEY-*`; A4); with saved digs, the notice `S-KEY-NOTICE` + ADD KEY above the stage on dig pages. Search shows `S-SB-NOKEY` + ADD KEY; Start a dig creates no dig (`S-SB-DIG-NOKEY`); a step that runs without a key fails as `cosine_key_missing` with ADD KEY, and a refused key as `cosine_key_rejected` with FIX KEY (`S-E-KEY-PARTIAL` + Re-run when picks had arrived); once a key works, Retry |
| No Discogs token | L, A and C disabled with reasons; facts `S-INS-FACTS-NOTOKEN`; map `S-MAP-NOHUB`, no token line (P8) |
| Pick without a video id | Row `S-ROW-NOVIDEO`; skipped by auto-advance; Space shows `S-LCD-NOVIDEO`; "Search YouTube Music ↗" in the inspector |
| Local server lost | Stage top banner `S-SRV-LOST`, after 10 s `S-SRV-LOST-START` or `S-SRV-LOST-SERVE`, LED down. Actions that need the server are disabled with the banner as reason; the player keeps playing the loaded track. On reconnect: banner gone, `S-T-RECONNECTED` |
| Playing track from another dig | L2 `S-LCD-FROM-DIG`; G opens that dig |
| Window under 1100 px | `S-NARROW`; video still 200 × 200 and visible; key strip visible |

---

## 10. Decisions

Choices for things the sources leave open, each picked as the option most consistent with the handoff.

| # | Question | Decision | Why |
|---|---|---|---|
| D1 | How do label, artist and person moves read? | "Sounds like the label {X}", "Sounds like the artist {X}", "Sounds like records {X} is credited on". No possessives | Possessives break on common names ending in "s" ("Sol Records"); "the label X" is decision 4's own wording ("sounds like this label"). Changes the wire example "Ilian Tape's records" only in form |
| D2 | Which "no hubs" wording, §09's or criterion 16's? | "No shared labels or people yet" | Criterion 16 is the test string; one wording everywhere |
| D3 | How does "radius = 1 − score" fit a 300 px map? | Proportional at one fixed scale: score 0.5 or below at 0.85R, minimum 0.08R | Raw 1 − score packs every pick into the inner 40%; one scale for every step keeps positions comparable and deterministic |
| D4 | Where are seeds drawn when there are several? | Stars on an 18 px inner ring at their sector's middle; dotted sector bounds; seed names at the rim | Makes "one sector per seed" readable without a legend entry |
| D5 | Is X (pass) built, given §08's "(X is out of slice)"? | Yes, key and row button | Criterion 17 requires every §08 key to work; judgments are keep\|pass in the handoff schema |
| D6 | Which steps does M use? | Kept tracks among this step's picks, wherever they were kept | Matches "the keeps in this step" as you see them on its rows |
| D7 | How is a dig named? | Its first seed's "Artist - Title", plus " + {n} more" for several. No rename in milestone 1 | Decision 1 needs a name; renaming isn't specified |
| D8 | How are steps numbered? | Creation order within the dig; a re-run takes the next number | "from step N" and the trail need stable numbers |
| D9 | What opens first? | Your last viewed dig and step; with no digs, the seed box | §03 note on P27: "the first screen is the seed search or your last dig" |
| D10 | Where are keeps listed, and "Nothing kept yet"? | A "Kept in this dig" list in the trail column, collapsed by default | The handoff puts "kept in step 2" "in the crate"; export is out of scope, the list isn't |
| D11 | What does "today's Daily" on the new-dig screen do? | A card with "Open in YT Music" and "See it in Feeds"; not openable as a dig | Feeds as digs aren't approved |
| D12 | How many seed chips? | 10 at most | The engine's seed budget is 10 |
| D13 | Does X on the playing track keep playing it? | It skips to the next track at once | Passing what you hear means "not this"; R13 makes X act on it |
| D14 | K on a track already kept? | No change, toast "Already kept"; the row ★ button toggles | A repeated key never undoes; un-keeping is an explicit button or Z |
| D15 | What does Space/double-click/▶ queue? | The step from that row to its end | §07 "Play from this row"; the queue still ends at the step's end (R14) |
| D16 | What does a re-run's "shows the difference" display? | Header counts (same, new, gone), "+ new" on rows, a "Not in this re-run" section, "also in" suppressed against the original, and "Re-run later as step N" on the original | Shows change without editing the old snapshot (decision 6) |
| D17 | Retry vs re-run; reload resume vs stop? | Retry in place only for 0-pick failed or stopped steps; otherwise Re-run (sibling). Server alive → resumed; server restarted → stopped | Nothing to protect in an empty step; a step with picks is a snapshot |
| D18 | Repeating an identical move? | Opens the existing step with a toast | Avoids duplicate steps; re-run is the explicit way to run it again |
| D19 | Parent of a Shift+M step? | The step you're viewing | Criterion 2's "step holding the origin" is undefined for keeps across steps; the viewed step keeps the trail readable |
| D20 | When does "as you left it" show? | On any open that isn't the automatic open right after you created the step | That's what "returning" means in S8 |
| D21 | T on a step's own origin with the same move? | Toast "That's this step's move. Use Re-run to run it again." | Avoids a child that duplicates its parent |
| D22 | Which navigations add history? | Step changes push; selection and List/Map replace | Back walks "the steps you viewed" (§08) |
| D23 | Are row buttons in the tab order? | No; keyboard uses Space, K, X, Enter and the inspector's buttons | Keeps Tab moving between regions as §08 says, while every action stays reachable |
| D24 | Key strip and keys on Feeds and Setup? | Strip on the Dig page only; there `/`, `?`, G, `[`, `]`, K, X, Z work | Those pages are forms; native keys must keep working |
| D25 | Do expanded trail stubs stay open? | Until reload | R4 collapses by default; re-collapsing on every move would fight you |
| D26 | Map arrows and the menu's Left/Right? | The map doesn't pan with arrows (drag pans; + − 0 zoom and fit; Up/Down select); inside menus Left/Right never seek | Left/Right are seek keys app-wide (R2) |
| D27 | Where do toasts go so focus is never hidden? | Inside the key strip line on the Dig page | WCAG 2.4.11; a floating toast can cover the focused row |
| D28 | The CRT overlay covers the video | The video frame stacks above the CRT layer | Nothing may overlay the player; the CRT toggle stays |
| D29 | List/Map on the new-dig screen? | Visible, disabled, with a reason | Frame F1 shows it; there's no step to map |
| D30 | What drives the spectrum? | Decorative animation while playing; static bars with reduced motion; hidden from screen readers | The embed exposes no audio; R5 says it moves only while music plays |
| D31 | Where does the Dig mode button go? | Your last Dig route | Returns you to your place |
| D32 | Layout below 1100 px? | Keep the 1100 grid, scroll horizontally past the 320 px centre minimum, hide the spectrum | "Usable without guarantees" with the player still 200 × 200 |
| D33 | Video size at wider windows? | 200 × 200 at every width | Meets the floor and keeps the faceplate at 224 px |
| D34 | Scrub bar? | YouTube's own controls (`controls=1`); no custom scrub bar | Free scrubbing inside the player, no overlay risk |
| D35 | Long LCD text? | Ellipsis, no marquee | The marquee was the job-log ticker the handoff removes |
| D36 | HUD counters and LCD lights? | Removed from the shell | Profile stats aren't the core any more; their warnings surface where they matter (seed box, Feeds) |
| D37 | Clock and next runs go to Feeds or Setup? | Feeds only | The handoff says Feeds; R5 said Feeds and Setup |
| D38 | Remember refusals? | Per video id for the browser session | Avoids re-trying a known refusal during a dig; nothing new to store |
| D39 | Show artwork? | A 56 px thumbnail in the inspector when Discogs has one | The handoff parses thumbnails; it helps recognition without becoming a page |
| D40 | Which facts offer "Sounds like it"? | Artist, label and credited people; not year or style | There are no year or style moves |
| D41 | Map camera per step? | Fit on every open | Positions are fixed; a fitted view makes returns look identical (criterion 14) |
| D42 | Paused LCD colour? | `--lcd-soft` without glow, plus "❚❚" | `--lcd-dim` text fails contrast; the glyph carries the state |
| D43 | Who counts toward a person hub? | Credited people on the release except the track's own artist | The artist move covers the artist; hubs are about shared credits |
| D44 | Concurrent move cap? | None in the client; show "waiting" if the server serialises | "Each step streams on its own"; the server owns pacing |

---

## 11. Conflicts for the owner

Two places where approved sources pull against each other. The build uses the default shown until you decide.

| # | Conflict | Default the build uses | The alternative |
|---|---|---|---|
| C1 | Decision 5 and criterion 13 say no click starts a move, and list only keys, Enter-menu items and the inspector's "Sounds like…". Approved R14 and S4 (step end offers hubs as "where next"), §09 and the handoff edge cases (an empty step shows "those moves as buttons"), and decision 6 (re-run) also need clickable controls that create steps. | Content clicks (rows, dots, hubs, islands, trail, facts, LCD title, empty space) never create a step. Buttons worded "Sounds like…" or "Go one step further", wherever they sit, count with the inspector's "Sounds like…", as do Re-run and Retry. The acceptance check tests criterion 13 that way. | Make where-next and empty-step items only select the hub or move and focus the inspector's "Sounds like…" button, adding a click. Re-run would still need its own button. |
| C2 | Decision 6 and criterion 14 say returning shows "exactly what you saw, judgments included" and "the same marks". The approved state model makes judgments global with the latest winning, and frame S8 shows a returned step 1 row reading "heard in step 2". | Rows, ranks, facts and dot positions are frozen in the snapshot. Kept, passed, heard and also-in marks are live, and name the step where they happened ("★ kept in step 3", "heard in step 2"). Criterion 14 is checked with no judging of the same tracks in between. | Freeze marks too, showing the judgment as it was when the step was last viewed. That contradicts global latest-wins judgments and S8. |
