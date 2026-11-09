# cosine-digger

## Try Digger

Digger finds tracks that sound like one you love and plays them while you dig. It runs on your Mac or Linux computer and opens in your browser.

```sh
curl -fsSL https://github.com/marcmuon/cosine-digger/releases/latest/download/start.sh | sh
```

Your browser then asks for one thing: a cosine.club API key. Accounts are free: make one at [cosine.club](https://cosine.club) and copy the key from your [account page](https://cosine.club/account/api). With only that key you can search, dig, play and keep tracks.

Digger keeps running after you close Terminal or the browser.

```text
cosine-digger stop     stops it
cosine-digger start    starts it again, also after a restart
cosine-digger status   says whether it's running, and where
```

## What it does

Dig for music you haven't heard yet. Start from a track you love: each step lists tracks that sound like it, found by [cosine.club](https://cosine.club) audio similarity. Listen in the built-in player, keep or pass, then follow a track, its label, its artist or a credited person to the next step. Your keeps feed two private YouTube Music playlists, **Digger Daily** and **Digger Weekly**, that Digger refreshes when you press **Run now**, or on a schedule you set up with `cosine-digger schedule install`.

It runs on your computer with your own keys. Labels, artists and credits come from [Discogs](https://www.discogs.com). The app is built for desktop browsers in windows 1100 px wide or more; narrower windows show a notice.

- **Dig.** A dig is a saved trail of steps. Each step is one move, such as "sounds like this track" or "sounds like its label", and every pick says why it's there.
- **Feeds.** About 20 new tracks for the daily playlist and 50 for the weekly one, written to private YouTube Music playlists that are replaced on each run. Nothing you own or were already served comes back.
- **Learns only from keep and pass.** Your keeps, your passes and your music library shape the feeds. Looking around never does.
- **Command line and agents.** `quick` turns a few tracks into a private playlist and a why-list in seconds; `/dig` does the same from an agent.

## Using the app

### Start

Run `cosine-digger start`. It opens Digger in your browser, at http://127.0.0.1:8765 unless you pick another port with `--port` (Digger remembers it). The first time, the app asks for your cosine.club key, then opens on **Start a dig**; after that it opens where you left off. Press `/` at any time to start a new dig. The player keeps playing.

### The screen

- **Faceplate** (top): the YouTube player, what's playing and which step it came from, the transport, **List** and **Map**, and the **Dig**, **Feeds** and **Setup** screens.
- **Trail** (left): the dig's name and its steps as a tree. Each step shows its number, what it sounds like, its move, ▶ if the playing track came from it, and its ★ keeps and ✕ passes. **Kept in this dig** lists your keeps. In windows under 1280 px the trail folds into a narrow rail; hover over it to open it.
- **Step list** (middle): the step you're viewing, best pick first.
- **Step map and inspector** (right): the map draws this step's picks (nearer = more alike) with the ■ labels and ◯ people that 2 or more picks share. The inspector shows what you selected.
- **Key strip** (bottom): the keys for where you are. `?` shows them all.

### Start a dig

1. Type an artist or a track in the seed box, or paste a Discogs, YouTube or cosine.club link.
2. Results come in two groups: **On cosine.club** first, then **In your library**. Library rows say whether cosine.club has the file: "your file · on cosine.club", "your file · not on cosine.club, …" or "your file · index check unavailable".
3. Press `Enter` to add the highlighted result as a chip. Add more seeds the same way, up to 10. `Backspace` in the empty box removes the last chip.
4. Press `Enter` on the empty box, or **Dig from N tracks ▸**.

If a seed isn't on cosine.club, step 1 uses indexed tracks by the same artist and says so. **Recent digs**, under the box, lists your five newest digs.

### Steps

A step is the result of one move: up to 25 near picks, plus up to 25 further picks (50 total) for a "one step further" move, ranked by nearness (higher means more alike). The header says what the step sounds like, such as "Sounds like the label Lobster Theremin", and which step it came from. Picks appear as they arrive.

A step is a snapshot. Open it again and you get the same picks in the same order; only your keeps, passes and "heard" marks change. **Re-run** asks again and adds a new step beside the old one, tagged **changed**: new picks get "+ new", picks that dropped out are listed under "Not in this re-run", and the old step says "Re-run later as step N". A step that failed or stopped before any picks arrived shows **Retry** instead.

### Listen

- `P` or **▶ Play step** plays the step from the top. `Shift+P` plays it after the current track.
- `Space` plays the selected track and the rest of the step after it; on the playing track it pauses or resumes. Double-clicking a row or a dot on a map plays it too.
- `[` and `]` go to the previous and next track. `←` and `→` seek 15 s; `Shift+→` seeks 60 s.
- A new step never starts playing by itself. When a step ends, the player shows "Step N finished. Where next:" with up to three moves to try.
- After 30 s of play a track is marked **heard**. Passed tracks are skipped.
- Some videos refuse to play outside YouTube. The player says so, offers **Open in YouTube Music**, and skips after 2 s. The inspector always links the selected track: **Open in YouTube Music ↗**, or **Search YouTube Music ↗** when there's no video.
- Playback pauses when you switch tabs or hide the window, and comes back paused after a reload. Press ▶ to go on.
- To keep listening in another tab, press **▶ Send to YT Music** under the dig's name. Digger makes the dig a private YouTube Music playlist (every step's tracks in order, each once, passes left out) and opens it in a new tab. **Open in YT Music** opens it again; **Update** brings it up to date after new steps. Sending a dig never changes your Daily or Weekly.
- If your browser blocks YouTube's cookies, YouTube can't see a Premium account, so ads may play.

### Selecting is not playing

Click a row, or use `↑` `↓`, to select a track. The selection drives the inspector, the highlight on the map, and every move. The player keeps playing its own step while you look around.

- `K` and `X` judge the **playing** track. With nothing playing, they judge the selection.
- Moves start from the **selection**, never from the playing track.
- `G`, or a click on the title in the player, jumps to the playing track: it opens that step and selects the track.

### The inspector

With a track selected, the inspector has **▶ Play**, **★ Keep**, **✕ Pass** and the YouTube Music link, then two sections:

- **Why it's here**: how this dig found the track, for example "Sounds like Florian T M Zeisig - 85805. Nearness 0.97, rank 2 of 25.", with its similarity to each seed.
- **Facts** (from Discogs): what Discogs says about the record: artist, label, year, credits and style, plus **Discogs release ↗**. Each artist, label and credit has **Sounds like it**; Shift-click goes one step further.

Select a ■ label or ◯ person on the map to see the picks they share. With nothing selected, the inspector sums up the step.

### Moves: Sounds like…

Select a track, then press a key, or press `Enter` (or the row's **Sounds like ▾**) for the **Sounds like…** menu. Each move adds a step under the one you're viewing.

- `T` **this track**: tracks that sound like it. About 3 s.
- `L` **its label**: sounds like the label. It reads the label's newest releases on Discogs (up to 20) and digs from up to 5 that cosine.club knows. About 10 to 30 s.
- `A` **its artist**: digs from the artist's own releases. About 4 to 8 s.
- `C` **a credited person**: a producer, remixer, engineer and so on. With more than one, a picker asks **Which credited person?** (`1` to `9` choose). About 10 to 30 s.

Add `Shift` to go **one step further** (about 30 s): after the near picks, the step adds a second batch that sounds like the labels, artists and people of the picks above. Its header ends with ", one step further".

On the map, select a ■ label or ◯ person and press `L` or `C`, or use **Sounds like it** in the inspector. Making the same move twice opens the step you already made.

### Sounds like your keeps

`M` digs from the keeps in this step; `Shift+M` from the keeps in the whole dig (your 10 most recent). With nothing selected, the inspector has the same two buttons: **Sounds like the keeps in this step (N)** and **Sounds like the keeps in this dig (N)**.

### Keep, pass, undo

- `K` keeps and `X` passes the playing track, or the selection when nothing plays. `X` on the playing track also skips to the next one.
- **★ Keep** and **✕ Pass** on a row or in the inspector judge that track. Press again to clear.
- `Z` (or `Cmd+Z`, or **Undo** in the message) undoes your last keep or pass, newest first, back to when you opened the page.

A keep or pass counts everywhere: other steps show it ("★ kept in step N"), and that track won't come back as a new pick.

### The whole dig as a map

Press `V`, or **Map** in the faceplate, to see the whole dig as one map, each step an island of its picks. Click another island to open that step, click a dot to select it, double-click a dot to play it. Drag to pan, scroll to zoom, double-click empty space to fit. The right column lists the open step. `V` again goes back to the list.

### Go back, switch digs

- Click a step in the trail to open it. It comes back as you left it.
- `U` opens the parent step. `Cmd+[` and `Cmd+]` walk back and forward through the steps you viewed.
- Click **▾** next to the dig's name to switch to a recent dig or start a **+ New dig**.
- Digs save as you go. A dig is named after its first seed.

### Feeds

The **Feeds** screen shows **Digger Daily** and **Digger Weekly**: how each one's last run went, and **▶ Open in YT Music** once the playlist exists.

- **Run now** builds the playlist, writes it to YouTube Music, reads it back and checks it.
- **Dry run** builds it without writing, so you can see the picks and why each was chosen.

Digger refreshes them when you press **Run now**. To refresh them on a schedule, run `cosine-digger schedule install` in Terminal and follow what it prints; it needs your cosine.club key saved in Digger (pasting it in the browser does that). Times and sizes are in Setup under **Schedule**: by default Daily at 06:00 with 20 tracks and Weekly on Monday at 06:00 with 50; sizes run from 5 to 100. A Digger started with `cosine-digger serve` or Docker also keeps that schedule itself while it runs, and missed runs catch up once on its next start.

### What teaches it your taste

Only **keep** and **pass**. Your keeps, your passes and your library shape Digger Daily and Digger Weekly. Searching, selecting, playing, "heard" marks and moves never change them, so you can dig anywhere without teaching it anything.

### Keys

| Key | What it does |
|---|---|
| `/` | Start a new dig (the player keeps playing) |
| `Enter` | Seed box: add the highlighted result, or start on an empty box. List or map: open **Sounds like…**. Trail: open the step |
| `Backspace` | Empty seed box: remove the last chip |
| `↑` `↓` | Select the previous or next pick; in the trail, move between steps |
| `Space` | Play the selected track; on the playing track, pause or resume |
| `P`, `Shift+P` | Play this step from the top, now or after the current track |
| `[` `]` | Previous or next track |
| `←` `→`, `Shift+→` | Seek 15 s back or forward; seek 60 s forward |
| `G` | Go to the playing track |
| `K`, `X` | Keep or pass the playing track (the selection when nothing plays) |
| `Z`, `Cmd+Z` | Undo the last keep or pass |
| `T` `L` `A` `C` | Sounds like this track, its label, its artist, a credited person |
| `Shift` + `T` `L` `A` `C` | The same, one step further |
| `M`, `Shift+M` | Sounds like the keeps in this step, or in the whole dig |
| `U` | Open the parent step |
| `Cmd+[` `Cmd+]` | Back or forward through the steps you viewed |
| `V` | Switch between List and Map |
| `+` `−` `0` | On a map: zoom in, zoom out, fit |
| `Esc` | Close the menu, picker, dig switcher or key sheet; otherwise clear the selection |
| `?` | Show every key |

On Feeds and Setup, only `/`, `?`, `G`, `[`, `]`, `K`, `X` and `Z` work. Letter keys don't work while you type in the seed box. If you click into the video, YouTube's own keys apply until you click outside it.

### Example: a dig from a file cosine.club doesn't have

"Florian T M Zeisig - 85805" (Planet Inc, 2024) is in the library but not in cosine.club's index.

1. Press `/` and type `85805`. Under **In your library** you get "Florian T M Zeisig - 85805", marked "your file · index check unavailable". Press `Enter` to make it a chip, then `Enter` again (or **Dig from 1 track ▸**).
2. Step 1 opens: "Sounds like Florian T M Zeisig - 85805", with the note "Your file 'Florian T M Zeisig - 85805' isn't on cosine.club. Using 2 indexed Florian T M Zeisig tracks instead: Been Listening A Lot, Aspire." **Choose a different seed** is there if you'd rather start elsewhere.
3. 25 picks arrive within seconds, and Discogs facts fill in over the next half minute. The top three are Manse - Lighght, John Cale - Living With You (Laurel Halo Remix) and Mauk Tenieb - Rózsaszín City (Tribute to SB). The step map shows ◯ Laurel Halo and ◯ Rashad Becker, each credited on 2 picks.
4. Press `P`. Manse - Lighght plays, and the player shows "from step 1: sounds like Florian T M Zeisig - 85805".
5. Click the John Cale row. Manse keeps playing. **Why it's here** says "Sounds like Florian T M Zeisig - 85805. Nearness 0.97, rank 2 of 25." **Facts** list the label Double Six (2), 2013, Laurel Halo (Remix) and Art Rock.
6. Press `K`. That keeps Manse - Lighght, the playing track, not the John Cale row you selected. To keep John Cale too, press **★ Keep** on its row.
7. With John Cale still selected, press `T`. Step 2 opens at once: "Sounds like John Cale - Living With You (Laurel Halo Remix)". Its 25 picks arrive within a few seconds, led by Wesqk Coast - Estate Nera and Oneohtrix Point Never - Rodl Glide. Manse plays on from step 1.
8. Press `U` to go back to step 1: the same 25 picks in the same order, "★ kept" on Manse, and "as you left it" in the header.
9. Press `V` to see both steps as one map, and `V` again for the list.

Manse - Lighght now counts toward Digger Daily and Digger Weekly, and it won't come back as a pick. Selecting John Cale and opening step 2 changed nothing there.

## Optional connections

Everything above works with only the cosine.club key. Open **Setup** (top right of the app) to add more. Press **Save settings** to keep what you enter.

1. **Discogs token**: get one at discogs.com/settings/developers. Label, artist and credited-person moves, the **Facts** panel and the shared labels and people on the map all need it. Without it you can still dig from seeds and tracks.
2. **YouTube Music login**, so Digger can write your playlists. Open music.youtube.com signed in, then developer tools → Network → filter `browse` → right-click a POST request → Copy → **Copy as cURL** (Firefox: Copy Value → Copy Request Headers). Paste it under **YouTube Music** and press **Log in**, or run `pbpaste | cosine-digger youtube login`. The headers stay in a private file (`~/.cosine-digger/ytmusic_auth.json`, mode 0600).
3. **Library**: enter any folder of tagged audio files as **Music folder** and press **Scan now**, or run `cosine-digger taste-scan ~/Music`. Scanning only reads your files and never changes them. Tracks you own never come back as picks or land in a playlist. **Fill missing labels from Discogs** looks up labels for library rows that have none.
4. **Exa API key**: adds fresh releases to your playlists.

Keys you paste in the browser are saved in Digger's data folder (~/.cosine-digger unless you set COSINE_DIGGER_HOME). Environment variables, and a `.env` file in the folder you run Digger from, win over them. Check everything with `cosine-digger doctor`, or press **Check** under **Health** in Setup.

## On iPhone and iPad

`ios/` holds a companion app that digs with the Digger on your computer: seed, audition, keep or pass, follow a label or person, and come back, on iPhone and iPad. Run `cosine-digger serve --companion`, then pair the app with the link it prints (`cosine-digger companion --copy` copies it). Build it from [ios/README.md](ios/README.md).

## Command line

```bash
cosine-digger start                      # start Digger in the background and open it; --port picks another port
cosine-digger status                     # whether it's running, its address and its data folders
cosine-digger stop                       # stop it; your digs and settings stay
cosine-digger serve                      # run it in this Terminal instead, until Ctrl+C; --open opens your browser
cosine-digger quick "Vladislav Delay - Ranta" "Conoco - Koski" "Humbros - Ressac"
cosine-digger quick "Basic Channel - BCD" --deep --playlist "Dub dig"
cosine-digger playlist daily --dry-run   # see the next Daily's picks and why
cosine-digger playlist weekly            # rebuild Digger Weekly now
cosine-digger schedule install           # launchd on macOS, a crontab line on Linux
cosine-digger doctor                     # check keys, YouTube login, music folder and database
```

`quick` turns a few tracks or albums into a private playlist and a why-list in seconds. `--deep` also follows labels and artists one hop. `--scorer` picks how seeds are combined: `rrf` (default, rank-based), `consensus` (rewards agreement across seeds) or `adventurous` (rewards tracks one seed loves). Without the app running, `schedule install` has your OS run `cosine-digger playlist due`.

More commands: `dig`, `chain-dig`, `label-dig`, `artist-dig`, `producer-dig`, `next` (review, save or pass), `saved`, `leads`, `recap`, `taste-dig`, `profile`. See `cosine-digger <command> --help`. `profile` shows the wider command-line profile, which also counts command-line digs; the feeds use only keeps, passes and your library.

### From an agent

`skills/dig/SKILL.md` is a Claude Code skill (`/dig`) that runs one `cosine-digger quick ... --json` call and replies with the playlist link and a why for every pick. Any agent that can run a shell command can use the same call.

## How it works

1. **Steps.** A step asks cosine.club for the tracks that sound closest to its seeds and combines the lists into one ranking. Label, artist and person moves first read Discogs (the label's newest releases, the artist's own releases, or the records a person is credited on) and dig from up to 5 that cosine.club knows. Picks skip anything in your library, anything the feeds already served, anything you kept or passed, muted labels and artists, and your command-line seeds. Each step is saved as it came back.
2. **Profile.** The feeds weigh artists, labels and tracks by your keeps (+), passes (−) and library, with recency decay.
3. **Feed picks.** Each run samples anchors from the profile (heavier ones more often, a fifth from the lighter half so it explores) and combines their cosine.club neighbours. Weekly also follows labels and artists one hop through Discogs. With an Exa key, fresh releases fill part of each list, once they resolve to a real cosine.club or YouTube Music track. Owned, served, kept and passed tracks are dropped, then picks are spread across artists and labels.
4. **Export.** The named private playlist is replaced, read back and verified track by track.

No LLM runs inside the product; every "why" comes from the real route a track was found by.

## Privacy

Your digs, keeps and passes, library rows, settings and YouTube login stay on your computer (`~/.cosine-digger/`, or `COSINE_DIGGER_HOME`). The app listens on 127.0.0.1 only: `cosine-digger start` always does, and `serve` does unless you change `COSINE_DIGGER_HOST` or run `serve --companion`. Companion mode listens on your network for the iPhone and iPad app and lets another device in only with the pairing token in `~/.cosine-digger/companion_token`; a paired device can search, dig, keep, pass and mark heard, but not read or change keys, logins or settings. Network calls go to cosine.club (similarity), Discogs (labels, artists and credits), YouTube Music (your playlists) and, if you set a key, Exa (fresh releases). To play tracks, the page embeds YouTube's player, loaded from youtube.com.

## From a checkout, or with Docker

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/). From a checkout of this repo:

```bash
uv sync
uv run cosine-digger start           # or `uv run cosine-digger serve --open` to keep it in this Terminal
```

`cosine-digger start` and `cosine-digger serve` read keys from a `.env` file in the folder you run them from, and from environment variables, which win over the file; [.env.example](.env.example) lists every setting. Keys from either show in Setup as read-only, with where they came from.

If you ran cosine-digger serve --port N before, run cosine-digger start --port N once. Digger remembers the port. Nothing inside a start Digger runs Daily and Weekly on a clock: they refresh when you press Run now, or on a schedule with cosine-digger schedule install.

Docker runs the app and the daily and weekly schedule, with data in a volume:

```bash
docker compose up -d                 # then open http://localhost:8765
```

To scan music in Docker, uncomment the two music lines in [compose.yaml](compose.yaml). The folder is mounted read-only.

## Development

```bash
uv sync --extra dev
uv run pytest -q
uv run cosine-digger serve --reload    # restarts on code changes; the schedule stays off
uv run scripts/slice_playthrough.py    # scripted journeys through the app in a browser (Playwright)
sh scripts/done_check.sh               # add --live for the checks that call real services
uv run python scripts/bench.py         # dig timings against live services
```

## Update

Run cosine-digger stop, then uv tool upgrade cosine-digger, then cosine-digger start. A Digger that's already running keeps the version it started with until it stops.

## Uninstall

To remove Digger: run cosine-digger status and note the data folders it lists. If you set up a schedule, run cosine-digger schedule uninstall and the command it prints. Then run cosine-digger stop and uv tool uninstall cosine-digger. Your digs and keys stay in those folders; delete them to remove those too.

## Credits

Built on [cosine.club](https://cosine.club) and [Discogs](https://www.discogs.com); this tool wouldn't exist without those teams. Deepest thanks to Pablo Alonso-Jiménez, Xavier Serra and Dmitry Bogdanov, [*Music Representation Learning Based on Editorial Metadata from Discogs*](https://ismir2022program.ismir.net/poster_243.html) (ISMIR 2022). YouTube Music access through [ytmusicapi](https://github.com/sigma67/ytmusicapi).

## License

GNU Affero General Public License v3 ([LICENSE](LICENSE)).
