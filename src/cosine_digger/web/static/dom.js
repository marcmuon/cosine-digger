// Product §8 wording, including the binding interaction-spec amendments.
// Plain strings only: callers insert the result as text, never markup.
export const strings = Object.freeze({
  "S-LCD-IDLE": "nothing playing yet",
  "S-LCD-IDLE-HINT": "Space plays the selected track · P plays this step",
  "S-LCD-IDLE-HINT-NOSEL": "▶ PLAY STEP or P plays this step · ↓ selects a track",
  "S-LCD-FROM": "from step {n}: {sentence with a lowercase first letter}",
  "S-LCD-FROM-DIG": "from {dig name}, step {n}: {sentence with a lowercase first letter}",
  "S-LCD-REFUSED": "Can't play here: the owner turned off embedding. Skipping in {s} s",
  "S-LCD-UNAVAIL": "Can't play here: this video isn't available. Skipping in {s} s",
  "S-LCD-NOVIDEO": "No video for this track. Skipping in {s} s",
  "S-LCD-OFFLINE": "The player can't reach YouTube. Track links still work.",
  "S-LCD-RESTORED": "Paused after reload. Press ▶ to resume.",
  "S-LCD-GESTURE": "Press ▶ to start playback.",
  "S-LCD-WAITING": "Waiting for more tracks from step {n}…",
  "S-LCD-UPNEXT": "Up next: step {n} from the top",
  "S-LCD-END": "Step {n} finished. Where next:",
  "S-LCD-WN-LABEL-ARIA": "Sounds like the label {Label}",
  "S-LCD-WN-PERSON-ARIA": "Sounds like records {Person} is credited on",
  "S-LCD-WN-TRACK-ARIA": "Sounds like {Artist - Title}",
  "S-LCD-WN-ARTIST": "its artist: {Artist}",
  "S-VID-NOSIGNAL": "NO SIGNAL",
  "S-LISTMAP-OFF": "Open a dig to switch views",
  "S-NARROW": "cosine-digger is built for desktop windows 1100 px and wider",
  "S-SRV-LED-OK": "Server: connected",
  "S-SRV-LED-DOWN": "Server: lost",
  "S-SRV-LOST": "Lost the local server. Your dig is saved; reconnecting…",
  "S-SRV-LOST-START": "Can't reach Digger at this address. Your digs are saved. In Terminal, cosine-digger start starts it again, or opens it where it's already running.",
  "S-SRV-LOST-SERVE": "Can't reach Digger at this address. Your digs are saved. Start Digger again the way you started it; this page reconnects when it's back.",
  "S-SKIP": "Skip to the step list",
  "S-SB-TITLE": "Start a dig",
  "S-SB-ASIDE": "add one seed or several",
  "S-SB-LEDE": "Start from a track you love.",
  "S-SB-PH-0": "Type an artist or a track, or paste a link",
  "S-SB-PH-N": "Add another seed, or press Enter to start",
  "S-SB-SEARCHING": "~ Searching cosine.club…",
  "S-SB-SEC-COSINE": "On cosine.club",
  "S-SB-SEC-LIB": "In your library",
  "S-SB-SEC-LIB-ASIDE": "(any scanned folder or importer)",
  "S-SB-LIB-IDX": "your file · on cosine.club",
  "S-SB-LIB-NOTIDX": "your file · not on cosine.club, so the dig would start from indexed tracks by {Artist} and say so",
  "S-SB-MORE": "… more matches",
  "S-SB-NOMATCH": "No match on cosine.club for '{query}'. Try artist and title, or paste a link.",
  "S-SB-LIB-NOMATCH": "No match in your library.",
  "S-SB-NOLIB": "Scan a music folder in Setup to start from your own files",
  "S-SB-NOKEY": "Add your cosine.club key to search.",
  "S-SB-LINK-ROW": "Use this link: {host/path, 48 characters max}",
  "S-SB-LINKS": "or paste a Discogs, YouTube or cosine.club link",
  "S-SB-ADDED": "added",
  "S-SB-DUP": "Already a seed.",
  "S-SB-MAX": "Up to 10 seeds per dig.",
  "S-SB-ENTER-WAIT": "Still searching. Press Enter again when a match appears.",
  "S-SB-EMPTY-ENTER": "Type an artist or a track first.",
  "S-SB-REMOVED": "Removed {chip text}",
  "S-SB-CHIP-X": "Remove {chip text}",
  "S-SB-CHIP-NOTIDX": "Not on cosine.club: the dig will use indexed tracks by {Artist}",
  "S-SB-BTN": "Dig from {n} tracks ▸",
  "S-SB-BTN-HINT": "or Enter on an empty box",
  "S-SB-STARTING": "Starting…",
  "S-SB-START-ERR": "Couldn't start the dig: {reason}. Your seeds are still here.",
  "S-SB-RECENT": "Recent digs",
  "S-SB-RECENT-META": "{when} · {s} steps · {k} kept",
  "S-SB-DAILY": "Today's Daily",
  "S-SB-DAILY-META": "{n} tracks · {Ddd D Mon}",
  "S-SB-DAILY-FEEDS": "See it in Feeds",
  // Public onboarding (step 3): search and Start a dig without a usable key, and cosine.club's answers.
  "S-SB-KEY-REJECTED": "cosine.club didn't accept your key. Fix it to search and dig.",
  "S-SB-UNREACHABLE": "Couldn't reach cosine.club. Check your connection.",
  "S-SB-LIB-STILL": "Library matches still work.",
  "S-SB-BUSY": "cosine.club is busy. Try again in {s} s.",
  "S-SB-BUSY-NOW": "cosine.club is busy.",
  "S-SB-SERVER-ERR": "cosine.club didn't answer: server error. Try again in {s} s.",
  "S-SB-SERVER-ERR-NOW": "cosine.club didn't answer: server error.",
  "S-SB-DIGGER-ERR": "Digger's search failed: {reason}.",
  "S-SB-DIG-NOKEY": "Digging needs your cosine.club key.",
  // The key screen (#connect), its notice for returning users, and the buttons that open it.
  "S-KEY-TITLE": "Connect cosine.club",
  "S-KEY-LEDE": "Digger finds similar tracks through cosine.club. Paste your API key to start digging.",
  "S-KEY-LABEL": "cosine.club API key",
  "S-KEY-PH": "Paste it here",
  "S-KEY-HELP": "No key yet? Make a free account at cosine.club, then copy the key from your account page ↗",
  "S-KEY-HELP-LINK": "your account page ↗",
  "S-KEY-BTN": "Connect and start digging",
  "S-KEY-CHECKING": "Checking…",
  "S-KEY-PRIVACY": "Digger keeps the key on this computer and sends it only to cosine.club.",
  "S-KEY-EMPTY": "Paste your key first.",
  "S-KEY-MALFORMED": "That key has a space or line break in it. Copy it again and paste it here.",
  "S-KEY-MALFORMED-ENV": "That key has a space or line break in it.",
  "S-KEY-REJECTED": "cosine.club didn't accept that key. Copy it again from your account page.",
  "S-KEY-UNREACHABLE": "Couldn't reach cosine.club to check the key. Check your connection, then try again.",
  "S-KEY-BUSY": "cosine.club asked Digger to wait {n} seconds. Try again then.",
  "S-KEY-SERVER-ERR": "cosine.club had a server error while checking the key. Try again in {n} seconds.",
  "S-KEY-ENV": "Using the key from {variable} (ends …{last4}), set where Digger was started. To change it, run cosine-digger stop, change that variable, then run cosine-digger start.",
  "S-KEY-ENV-EMPTY": "{variable} is set but empty where Digger was started. To use a key, run cosine-digger stop, set that variable to your key or remove it, then run cosine-digger start.",
  "S-KEY-OK": "Connected to cosine.club.",
  "S-KEY-SAVE-FAILED": "The key works, but Digger couldn't save it: {reason}. Try again.",
  "S-KEY-DIGGER-ERR": "Digger couldn't check the key: {reason}. Try again.",
  "S-KEY-SUPERSEDED": "Another key was saved while this one was checked.",
  "S-KEY-NOTICE": "Add your cosine.club key to search and start new digs. Your saved digs still open and play.",
  "S-KEY-ADD": "Add key",
  "S-KEY-FIX": "Fix key",
  "S-KEY-TRY-AGAIN": "Try again",
  "S-HDR-SEED-1": "Sounds like {Artist - Title}",
  "S-HDR-SEED-N": "Sounds like {n} seeds: {s1}, {s2}, {s3}",
  "S-HDR-TRACK": "Sounds like {Artist - Title}",
  "S-HDR-LABEL": "Sounds like the label {Label}",
  "S-HDR-ARTIST": "Sounds like the artist {Artist}",
  "S-HDR-PERSON": "Sounds like records {Person} is credited on",
  "S-HDR-THESE-STEP": "Sounds like your {k} keeps in step {n}",
  "S-HDR-THESE-DIG": "Sounds like your {k} keeps in this dig",
  "S-HDR-FURTHER": ", one step further",
  "S-HDR-META": "{HH:MM} · {c} tracks · {k} kept · {p} passed",
  "S-HDR-META-RETURN": "{HH:MM}, as you left it · {c} tracks · {k} kept · {p} passed",
  "S-HDR-FROM-TRACK": "from step {p}: {Artist - Title}",
  "S-HDR-FROM-LABEL": "from step {p}: the label of {Artist - Title}",
  "S-HDR-FROM-ARTIST": "from step {p}: the artist of {Artist - Title}",
  "S-HDR-FROM-PERSON": "from step {p}: {role} on {Artist - Title}",
  "S-HDR-FROM-ORIGIN": "from step {p}: where it started, {name}",
  "S-HDR-FROM-HUB-LABEL": "from step {p}: a label {k} picks share",
  "S-HDR-FROM-HUB-PERSON": "from step {p}: a person credited on {k} picks",
  "S-HDR-FROM-THESE": "from step {p}: {k} keeps: {t1}, {t2}, {t3} (and {m} more)",
  "S-HDR-PROGRESS": "listening for neighbours · {i} of {n}",
  "S-HDR-WAITING": "waiting for another move to finish",
  "S-HDR-RESOLVING": "reading Discogs releases for {name}…",
  "S-HDR-TAG-RUNNING": "~ running",
  "S-HDR-TAG-WAITING": "~ waiting",
  "S-HDR-TAG-FAILED": "! failed",
  "S-HDR-TAG-STOPPED": "‖ stopped",
  "S-HDR-TAG-CHANGED": "changed",
  "S-HDR-RERUN": "+ Re-run of step {n} from {HH:MM}: {a} the same, {b} new, {c} gone.",
  "S-HDR-RERUN-RUN": "+ Re-run of step {n} from {HH:MM} · comparing as tracks arrive",
  "S-HDR-RERUN-LATER": "Re-run later as step {m}",
  "S-N-SWAP-FILE": "Your file '{seed}' isn't on cosine.club. Using {n} indexed {Artist} tracks instead: {titles}.",
  "S-N-SWAP-TEXT": "'{seed}' isn't on cosine.club. Using {n} indexed {Artist} tracks instead: {titles}.",
  "S-N-CLOSEST": "'{seed}' matched '{Artist - Title}', the closest version on cosine.club.",
  "S-N-DROPPED": "'{seed}' isn't on cosine.club and has no stand-in, so this step uses your other seeds.",
  "S-N-UNCHECKED": "'{seed}' wasn't checked: this step used all its cosine.club lookups on your other seeds.",
  "S-N-PARTIAL": "{k} of your {n} seeds are on cosine.club.",
  "S-N-NONE": "None of your seeds are on cosine.club, and it has no tracks by {Artist(s)} yet. Try another track by them, or paste a cosine.club link.",
  "S-N-DUP": "'{Artist - Title}' is in more than one of your seeds; it counts once.",
  "S-N-ALBUM": "The album '{Album}' has {t} tracks; {u} of them are used so every seed gets a turn (10 seed tracks at most).",
  "S-N-KEEPS-CAP": "Using your 10 most recent keeps of {k}.",
  "S-N-LABEL-FEW": "Only {m} of the newest releases on {Label} are on cosine.club, so this step leans on them.",
  "S-N-FACTS-BUDGET": "This step used its Discogs lookups, so some picks have no facts.",
  "S-N-FACTS-DOWN": "Discogs isn't answering, so some picks have no facts.",
  "S-N-FACTS-STOPPED": "The step stopped before every pick had facts.",
  "S-E-EMPTY": "Nothing came back for this move. Try {alt 1}, or {alt 2}.",
  "S-E-FAIL-COSINE": "cosine.club didn't answer: {rate limited | server error | timed out | unreachable}. Nothing was lost.",
  "S-E-FAIL-DISCOGS": "Discogs didn't answer, so {the label's releases | the artist's records | their credits} couldn't be read: {reason}. Nothing was lost.",
  "S-E-FAIL-OTHER": "This move stopped with an error: {plain message, secrets redacted}. Nothing was lost.",
  "S-E-FAIL-L2": "Your other steps and the player keep working.",
  "S-E-RETRY": "Retry",
  "S-E-RETRY-WAIT": "Retry in {s} s",
  "S-E-RERUN-WAIT": "Re-run in {s} s",
  "S-E-STOPPED": "This move stopped when the app closed. {i} of {n} tracks had arrived; they're kept.",
  "S-E-NOTOKEN-L": "Sounds like its label needs a Discogs token. Add one in Setup.",
  "S-E-NOTOKEN-A": "Sounds like its artist needs a Discogs token. Add one in Setup.",
  "S-E-NOTOKEN-C": "Sounds like a credited person needs a Discogs token. Add one in Setup.",
  // Public onboarding (step 3): key errors after some picks had arrived, and the Discogs way out.
  "S-E-KEY-PARTIAL": "cosine.club stopped accepting your key, so this step stopped early. {i} of {n} tracks had arrived; they're kept.",
  "S-E-CONNECT-DISCOGS": "Connect Discogs",
  "S-ROW-TITLE": "{Artist - Title} · Digger score {0.79}",
  "S-ROW-REFUSED": "! can't play here",
  "S-ROW-UNAVAIL": "! isn't available",
  "S-ROW-NOVIDEO": "no video",
  "S-ROW-KEPT": "★ kept",
  "S-ROW-KEPT-IN": "★ kept in step {n}",
  "S-ROW-PASSED": "✕ passed",
  "S-ROW-PASSED-IN": "✕ passed in step {n}",
  "S-ROW-NEW": "+ new",
  "S-ROW-HEARD": "heard",
  "S-ROW-HEARD-IN": "heard in step {n}",
  "S-ROW-ALSO": "also in step {n}",
  "S-ROW-ALSOON": "■ also on {Label}",
  "S-ROW-WHY-ALL": "close to all {n} seeds",
  "S-ROW-WHY-K": "close to {k} of your {n} seeds",
  "S-ROW-WHY-1": "close to {seed Artist - Title}",
  "S-ROW-WHY-FURTHER": "one step further: sounds like {subject}",
  "S-ROW-DIVIDER": "One step further · sounds like the labels, artists and people of the picks above",
  "S-ROW-DIVIDER-RUN": "~ {i} so far",
  "S-ROW-GONE-DIVIDER": "Not in this re-run · {c} tracks step {n} had",
  "S-ROW-BTN": "▶ Play · ★ Keep / ★ Kept · ✕ Pass / ✕ Passed · Dig from ▾",
  "S-TR-META": "{s} steps · {k} kept",
  "S-TR-SWITCH": "Switch dig",
  "S-TR-NEW": "+ New dig",
  "S-SET-YT-CHROME-WAIT": "Waiting for macOS: click Allow when it asks to use “Chrome Safe Storage”.",
  "S-TR-SWITCH-ROW": "{dig name} · {when} · {s} steps · {k} kept",
  "S-TR-ROOT-1": "sounds like it",
  "S-TR-ROOT-N": "sounds like {n} seeds",
  "S-TR-THESE": "{k} keeps",
  "S-TR-STUB": "+ {n} more steps",
  "S-TR-TIP": "{Sentence} · {c} tracks · {k} kept · {p} passed · {HH:MM}",
  "S-TR-KEPT-H": "Kept in this dig ({k})",
  "S-TR-KEPT-ROW": "{Artist - Title} · step {n}",
  "S-TR-KEPT-EMPTY": "Press K while a track plays to keep it",
  "S-TR-LEGEND": "▶ holds the playing track · ~ running",
  "S-TR-YT-SEND": "▶ Send to YT Music",
  "S-TR-YT-SEND-TIP": "Make a private YouTube Music playlist of this dig and open it in a new tab: every step's tracks in order, passes left out. It plays on when you switch tabs; Digger's player pauses.",
  "S-TR-YT-TAB": "Sending to YouTube Music…",
  "S-TR-YT-TAB-WAIT": "Making a private YouTube Music playlist of your dig. It opens here when it's ready.",
  "S-TR-YT-SENDING": "Sending…",
  "S-TR-YT-WORKING": "Making a private playlist of this dig…",
  "S-TR-YT-UPDATING": "Updating the playlist…",
  "S-TR-YT-OPEN": "▶ Open in YT Music",
  "S-TR-YT-OPEN-TIP": "Open {dig name} in YouTube Music, in a new tab",
  "S-TR-YT-UPDATE": "Update",
  "S-TR-YT-UPDATE-TIP": "Bring the playlist up to date: its tracks become this dig as it is now.",
  "S-TR-YT-CONNECT": "Connect YT Music",
  "S-TR-YT-CONNECT-TIP": "Opens Setup to connect YouTube Music, then send this dig.",
  "S-TR-YT-DONE": "{n} tracks in the playlist",
  "S-TR-YT-PASSED": "{p} passed left out",
  "S-TR-YT-NOVIDEO": "{v} with no video",
  "S-TR-YT-MISSING": "{m} YouTube Music didn't take",
  "S-TR-YT-UNVERIFIED": "YouTube Music hasn't shown every track yet. Open it to check.",
  "S-TR-YT-SENT-AT": "Sent {when}",
  "S-TR-YT-BEHIND": "{n} steps aren't in it yet",
  "S-MAP-TITLE": "Map of step {n}",
  "S-MAP-LEGEND": "nearer = more alike · ■ label ◯ person shared by 2+ picks",
  "S-MAP-NOHUB": "No shared labels or people yet",
  "S-MAP-DISCOGS-DOWN": "! Discogs isn't answering, so shared labels and people are missing.",
  "S-MAP-FACTS-PARTIAL": "! Some picks have no facts, so shared labels and people may be incomplete.",
  "S-MAP-FROM": "◁ from step {p}",
  "S-MAP-TIP-PICK": "{Artist - Title} · Digger score {0.79}",
  "S-MAP-TIP-LABEL": "{Label} · on {k} picks here",
  "S-MAP-TIP-PERSON": "{Person} · credited on {k} picks here ({roles})",
  "S-TERR-TITLE": "The whole dig, drawn as a map",
  "S-TERR-LIST-H": "Step {n} · {c} tracks",
  "S-INS-WHY-H": "Why it's here",
  "S-INS-WHY-1": "{Sentence}. Rank {r} of {c}.",
  "S-INS-WHY-N": "Close to {k} of your {n} seeds, by cosine.club similarity: {scores}. Rank {r} of {c}.",
  "S-INS-WHY-FURTHER": "One step further: sounds like {subject}, which is on {k} picks above.",
  "S-INS-ALSO-H": "Also in",
  "S-INS-ALSO-ROW": "Step {n} · {sentence} · rank {r}",
  "S-INS-HEARD": "Heard in step {n} at {HH:MM}",
  "S-INS-FACTS-H": "Facts",
  "S-INS-FACTS-ASIDE": "from Discogs",
  "S-INS-FACTS-LOADING": "~ Loading facts from Discogs ({i} of {n} done for this step)",
  "S-INS-FACTS-MISSING": "Discogs has no release for this track.",
  "S-INS-FACTS-DOWN": "! Facts unavailable: Discogs isn't answering.",
  "S-INS-FACTS-BUDGET": "Facts not fetched: this step used its Discogs lookups.",
  "S-INS-FACTS-STOPPED": "Facts not fetched: the step stopped first.",
  "S-INS-FACTS-NOTOKEN": "Label, year and credits come from Discogs (not connected).",
  "S-INS-ALSO-ON": "Also on {Label}: this track really is on the label this step sounds like.",
  "S-INS-CREDITS-MORE": "+ {k} more credits",
  "S-INS-DISCOGS": "Discogs release ↗",
  "S-INS-SOUNDS": "Dig from ▸",
  "S-INS-SOUNDS-FAR": "Dig one step further ▸",
  "S-INS-DIG-TIP": "Next step: tracks that sound like {target}",
  "S-INS-DIG-FAR-TIP": "Next step, one step further out (slower): tracks that sound like {target}",
  "S-INS-DIG-T-A": "{name}'s records",
  "S-INS-DIG-T-L": "records on {name}",
  "S-INS-DIG-T-C": "records {name} is credited on",
  "S-INS-HUB-LABEL": "{k} picks here are on this label.",
  "S-INS-HUB-PERSON": "Credited on {k} picks here: {roles}.",
  "S-INS-HUB-ALSO": "Also in step {n} ({k} picks)",
  "S-INS-ORIGIN-H": "Where step {n} started",
  "S-INS-ORIGIN-SEED": "{Artist - Title} · {k} picks sit closest to it",
  "S-INS-ORIGIN-FROM": "from step {p}",
  "S-INS-SUM-COUNTS": "{c} tracks · {k} kept · {p} passed · {h} heard",
  "S-INS-SUM-HUBS": "Shared here: ■ {Label} ({k}) · ◯ {Person} ({k})",
  "S-INS-SUM-FACTS": "Facts {i} of {n}",
  "S-INS-SUM-M": "Sounds like the keeps in this step ({k})",
  "S-INS-SUM-SHIFTM": "Sounds like the keeps in this dig ({k})",
  "S-INS-SUM-KEEPS": "Sounds like your keeps ({k})",
  "S-INS-SUM-HINT": "Click a track to see why Digger picked it.",
  "S-MENU-TITLE": "Next step: tracks that sound like…",
  "S-MENU-FAR-MODE": "One step further (slower)",
  "S-MENU-FROM": "from: {Artist - Title}",
  "S-MENU-T": "this track · about 3 s",
  "S-MENU-L": "its label: {Label} · about 10 to 30 s",
  "S-MENU-A": "its artist: {Artist} · about 4 to 8 s",
  "S-MENU-C": "a credited person ▸ · about 10 to 30 s",
  "S-MENU-HUB-L": "the label {Label} · about 10 to 30 s",
  "S-MENU-HUB-C": "records {Person} is credited on · about 10 to 30 s",
  "S-MENU-FOOT": "Shift + key: go one step further, slower (about 30 s)",
  "S-MENU-FAR-ARIA": "{item}, one step further",
  "S-MENU-LOADING": "(facts loading)",
  "S-MENU-NOLABEL": "(none listed)",
  "S-MENU-NONE": "(none listed)",
  "S-MENU-NOTOKEN": "(needs a Discogs token)",
  "S-PICK-LABEL": "Which label?",
  "S-PICK-ARTIST": "Which artist?",
  "S-PICK-PERSON": "Which credited person?",
  "S-KEYS-TITLE": "Keys",
  "S-KEYS-FOOT-1": "K and X judge the playing track; with nothing playing, the selected one.",
  "S-KEYS-FOOT-2": "Letter keys don't work while you type in the search box.",
  "S-KEYS-FOOT-3": "If you click into the video, YouTube's own keys apply until you click outside it.",
  "S-KS-SEARCH": "Enter adds a seed · Enter on an empty box starts · Backspace drops last chip · ? keys",
  "S-KS-LIST": "↑↓ select · Space play · K keep the playing track · T L A C dig from… · ? keys",
  "S-KS-TRAIL": "↑↓ move · Enter open step · U parent step · Cmd+[ back · ? keys",
  "S-KS-MAP": "↑↓ select · Enter dig from… · + − zoom, 0 fit · V whole dig · ? keys",
  "S-KS-INSPECTOR": "Tab next button · Enter press it · T L A C dig from… · ? keys",
  "S-KS-TERR": "V list view · click a step to open it · L sounds like this label · ? all keys",
  "S-KS-MENU": "T L A C choose · Shift goes one step further · Esc close · ? keys",
  "S-KS-IFRAME": "Keys go to YouTube while the video has focus. Click outside the video to get them back.",
  "S-T-KEPT": "Kept {Artist - Title}. Daily and Weekly look for more like your keeps. Z undoes it.",
  "S-T-KEPT-WAS": "Kept {Artist - Title} (it was passed). Z undoes it.",
  "S-T-PASSED": "Passed {Artist - Title}. It stays out of your Daily and Weekly. Z undoes it.",
  "S-T-ALREADY-KEPT": "Already kept.",
  "S-T-ALREADY-PASSED": "Already passed.",
  "S-T-UNDO-KEEP": "Undid the keep on {Artist - Title}.",
  "S-T-UNDO-PASS": "Undid the pass on {Artist - Title}.",
  "S-T-NOUNDO": "Nothing to undo.",
  "S-T-NOTARGET-K": "Play or select a track to keep it.",
  "S-T-NOTARGET-X": "Play or select a track to pass it.",
  "S-T-NOSEL": "Select a track first, or press G to select the playing track.",
  "S-T-SPACE-NOSEL": "Select a track first, or press P to play this step.",
  "S-T-NOTHINGPLAYING": "Nothing is playing.",
  "S-T-FACTS-LOADING": "Its label isn't known yet: facts are still loading.",
  "S-T-FACTS-LOADING-C": "Its credits aren't known yet: facts are still loading.",
  "S-T-NOLABEL": "Discogs lists no label for this track.",
  "S-T-NOPERSON": "Discogs lists no credited people for this track.",
  "S-T-WRONGHUB-PERSON": "That's a person. Press C for records they're credited on.",
  "S-T-WRONGHUB-LABEL": "That's a label. Press L to sound like it.",
  "S-T-NOKEEPS-STEP": "Nothing kept in this step yet. Press K while a track plays to keep it.",
  "S-T-NOKEEPS-DIG": "Nothing kept in this dig yet. Press K while a track plays to keep it.",
  "S-T-DUPMOVE": "You already made this move: step {n}.",
  "S-T-SAMEMOVE": "That's this step's move. Use Re-run to run it again.",
  "S-T-ROOT": "Step 1 is where this dig starts.",
  "S-T-MOVE-ERR": "Couldn't start the move: {reason}.",
  "S-T-JUDGE-ERR": "Couldn't save that {keep | pass}: {reason}. It's undone; try again.",
  "S-T-RECONNECTED": "Reconnected to the local server.",
  "S-T-YT-SENT": "{dig name} is in YouTube Music: {n} tracks.",
  "S-T-YT-ERR": "Couldn't send {dig name} to YouTube Music: {reason}",
  "S-T-YT-LOGIN": "Connect YouTube Music in Setup to send {dig name}.",
  "S-T-YT-REJECTED": "YouTube Music signed Digger out. Log in again in Setup to send {dig name}.",
  "S-AN-STEP-START": "Step {n} started: {sentence}",
  "S-AN-STEP-DONE": "Step {n} finished: {c} tracks",
  "S-AN-STEP-FAIL": "Step {n} failed: {reason}",
  "S-AN-PLAYING": "Now playing {Artist - Title}, from step {n}",
  "S-FEEDS-TITLE": "Feeds",
  "S-FEEDS-LEDE": "Digger writes two private YouTube Music playlists from your keeps, your passes and your library, and refreshes them on schedule. Looking around in a dig never changes them. Nothing you own or were already served, ever.",
  "S-FEEDS-CLOCK": "LOCAL TIME {HH:MM}",
  "S-FEEDS-NEXT-DAILY": "NEXT DAILY {DDD HH:MM}",
  "S-FEEDS-NEXT-WEEKLY": "NEXT WEEKLY {DDD HH:MM}",
  // S12 Feeds under start (scheduler off), from the report.
  "S-FEEDS-LEDE-START": "Digger can write two private YouTube Music playlists from your keeps, your passes and your library. Looking around in a dig never changes them.",
  "S-FEEDS-HOW-START": "Digger refreshes them when you press Run now. To refresh them on their own while Digger runs, start it with cosine-digger start --scheduler.",
  "S-FEEDS-LAST": "Last run: {when}",
  "S-FEEDS-LAST-NONE": "none yet",
  "S-FEEDS-CONNECT-YT": "Connect YouTube Music",
  // S12 copy deviations: no report row covers these states.
  "S-FEEDS-NO-RUN": "No run yet.",
  "S-FEEDS-LOGIN-MISSING": "YouTube Music isn't connected.",
  "S-SET-LIB-HELP": "Any folder of tagged audio files works; Plex is optional. Scanning only reads your files and never changes their tags. Tracks you own never come back as picks or land in a playlist.",
  "S-SET-FILL-LABEL": "Missing labels",
  "S-SET-FILL-BTN": "Fill missing labels from Discogs",
  "S-SET-FILL-IDLE": "{n} of {m} tracks in your library have no label.",
  "S-SET-LIB-SUM": "Your library: {n} tracks {from}.",
  "S-SET-LIB-SUM-MIX": "Your library: {n} tracks ({parts}).",
  "S-SET-LIB-SUM-FILE": "from your music folder",
  "S-SET-LIB-SUM-IMP": "from {Source}, imported {date}",
  "S-SET-LIB-SUM-IMP-NODATE": "from {Source}",
  "S-SET-LIB-SUM-EMPTY": "Your library is empty. Scan a music folder below to add tracks.",
  "S-SET-FILL-NONE": "Every track in your library has a label.",
  "S-SET-FILL-RUN": "Looking up labels on Discogs: {i} of {n}, about {t} left.",
  "S-SET-FILL-DONE": "✓ Filled {k} labels. Discogs had no label for {u} tracks.",
  "S-SET-FILL-STOPPED": "Stopped after {i} of {n}; {k} labels filled. The next run picks up here.",
  "S-SET-FILL-ERR": "! Discogs isn't answering. {k} labels filled so far; try again later.",
  "S-SET-FILL-NOTOKEN": "Needs a Discogs token (Discogs, above), so this is skipped.",
  "S-SET-FILL-NOLIB": "Scan a folder first.",
  "S-SET-FILL-BUDGET": "Uses up to {b} Discogs lookups per run, about 1 per second. You can stop at any time.",
  // S12 capability cards and Health (first-run step 4): the report's copy, then (after the
  // marker) the wording code needed where the report has no row; S12-report.md lists those.
  "S-SET-NOT-CONNECTED": "Not connected",
  "S-SET-CHECKING": "Checking…",
  "S-SET-CONNECTED": "Connected",
  "S-SET-COSINE-OK": "Connected · key ends …{last4}",
  "S-SET-COSINE-MISSING": "Needs a key",
  "S-SET-COSINE-REJECTED": "Key rejected",
  "S-SET-SOURCE-ENV": "From {env}",
  "S-SET-DISCOGS-OK": "Connected · token ends …{last4}",
  "S-SET-DISCOGS-REJECTED": "Discogs didn't accept that token. Make a new one at discogs.com/settings/developers.",
  "S-SET-DISCOGS-UNREACHABLE": "Couldn't reach Discogs to check the token. Try again.",
  "S-SET-DISCOGS-MALFORMED": "This Discogs token has a space or line break in it. Paste it again.",
  "S-SET-ENV-MALFORMED": "This {thing} has a space or line break in it. To fix it, run cosine-digger stop, change {env}, then run cosine-digger start.",
  "S-SET-YT-REJECTED": "YouTube Music didn't accept this login. Copy fresh headers and log in again.",
  "S-SET-YT-UNREACHABLE": "Couldn't reach YouTube Music to check the login. Try again.",
  "S-SET-YT-SAVED": "Login saved",
  "S-SET-MUSIC-NONE": "No folder yet",
  "S-SET-EXA-OK": "Connected · key ends …{last4}",
  "S-SET-EXA-REJECTED": "Exa didn't accept that key.",
  "S-SET-EXA-UNREACHABLE": "Couldn't reach Exa to check the key. Try again.",
  "S-SET-HEALTH-NEEDED": "Needed to dig: cosine.club key ✓",
  "S-SET-HEALTH-OFF": "not connected",
  "S-SET-CONNECT": "Connect",
  // S12 copy deviations: no report row covers these states.
  "S-SET-CHANGE": "Change",
  "S-SET-ADD-KEY": "Add key",
  "S-SET-FIX-KEY": "Fix key",
  "S-SET-COSINE-UNREACHABLE": "Couldn't reach cosine.club to check the key. Try again.",
  "S-SET-EXA-MALFORMED": "This Exa key has a space or line break in it. Paste it again.",
  "S-SET-TOKEN-SAVED": "Token saved",
  "S-SET-KEY-SAVED": "Key saved",
  "S-SET-BUSY": "{service} asked Digger to wait {s} seconds. Try again then.",
  "S-SET-SERVER-ERR": "{service} didn't answer: server error. Try again in {s} s.",
  "S-SET-SAVE-FAILED": "The {thing} works, but Digger couldn't save it: {reason}. Try again.",
  "S-SET-EMPTY": "Paste your {thing} first.",
  "S-SET-HEALTH-DB": "Needed to dig: taste database · can't open it",
  "S-ABOUT-TITLE": "About",
  "S-ABOUT-START": "Digger runs as a small server on this computer. It keeps running when you close this tab or Terminal, and stops when you run cosine-digger stop or restart the computer. To open it again later, run cosine-digger start; cosine-digger status shows whether it's running and its address. Your settings are in {data folder} and your digs and keeps in {database file}.",
  "S-ABOUT-SERVE": "Digger is running where you started cosine-digger serve, in a Terminal window or a container, and stops when that stops. Your settings are in {data folder} and your digs and keeps in {database file}.",
  "S-ABOUT-2": "This app embeds YouTube's player to play tracks. YouTube Terms of Service · Google Privacy Policy",
  "S-ABOUT-3": "If your browser blocks YouTube's cookies, YouTube can't see your Premium account, so ads may play.",
  "S-ABOUT-RANK": "How Digger ranks: each step asks cosine.club for the tracks nearest each origin, merges those lists by rank so picks near several origins rise, and hides tracks you own, kept, passed, muted or were already served. The Digger score is that merged order, scaled so the strongest candidate scores 1.00, even if the step hid it; picks one step further are scaled on their own. Similarity numbers are cosine.club's own.",
  "S-INS-K-TRACK": "Track",
  "S-INS-K-LABEL": "Label",
  "S-INS-K-PERSON": "Person",
  "S-INS-K-ORIGIN": "Origin",
  "S-INS-K-STEP": "Step {n}",
  "S-T-NOTOKEN-L": "Sounds like its label needs a Discogs token. Add one in Setup.",
  "S-T-NOTOKEN-A": "Sounds like its artist needs a Discogs token. Add one in Setup.",
  "S-T-NOTOKEN-C": "Sounds like a credited person needs a Discogs token. Add one in Setup.",
  "S-T-WRONGHUB-LABEL-T": "That's a label. Press L to sound like it.",
  "S-T-WRONGHUB-LABEL-A": "That's a label. Press L to sound like it.",
  "S-T-WRONGHUB-PERSON-T": "That's a person. Press C for records they're credited on.",
  "S-T-WRONGHUB-PERSON-A": "That's a person. Press C for records they're credited on.",
  "S-E-EMPTY-BTN-TRACK": "Sounds like this track",
  "S-E-EMPTY-BTN-LABEL": "Sounds like its label",
  "S-E-EMPTY-BTN-ARTIST": "Sounds like its artist",
  "S-E-EMPTY-BTN-PERSON": "Sounds like a credited person",
  "S-E-EMPTY-BTN-FURTHER": "Go one step further",
  "S-E-EMPTY-BTN-SEED": "Choose a different seed",
  "S-SB-BTN-TRACK": "Dig from {n} track ▸",
  "S-SB-BTN-TRACKS": "Dig from {n} tracks ▸",
  "S-SB-BTN-SEED": "Dig from {n} seed ▸",
  "S-SB-BTN-SEEDS": "Dig from {n} seeds ▸",
  "S-HDR-SEED-MORE": "Sounds like {n} seeds: {s1}, {s2}, {s3} and {k} more",
  "S-HDR-PROGRESS-FURTHER": "going one step further · {i} so far",
  "S-N-SWAP-FILE-1": "Your file '{seed}' isn't on cosine.club. Using 1 indexed {Artist} track instead: {t1}.",
  "S-E-STOPPED-EMPTY": "This move stopped when the app closed before any tracks arrived.",
  "S-ROW-ALSO-MANY": "also in steps {n}, {m}",
  "S-ROW-PLAY": "▶ Play",
  "S-ROW-KEEP": "★ Keep",
  "S-ROW-KEEP-ON": "★ Kept",
  "S-ROW-PASS": "✕ Pass",
  "S-ROW-PASS-ON": "✕ Passed",
  "S-ROW-SOUNDS": "Dig from ▾",
  "S-ROW-PLAY-TIP": "Play this track, then the rest of this step (Space)",
  "S-ROW-KEEP-TIP": "Keep it (K): Daily and Weekly look for more like your keeps",
  "S-ROW-KEEP-ON-TIP": "Kept. Click again to take it back",
  "S-ROW-PASS-TIP": "Pass on it (X): it stays out of your Daily and Weekly",
  "S-ROW-PASS-ON-TIP": "Passed. Click again to take it back",
  "S-ROW-DIG-TIP": "Dig from this track (Enter): the next step finds tracks that sound like it, its artist, its label or a credited person",
  "S-MAP-NOHUB-LOADING": "No shared labels or people yet · facts {i} of {n}",
  "S-INS-WHY-ALL": "Close to all {n} seeds, by cosine.club similarity: {scores}. Rank {r} of {c}.",
  "S-INS-WHY-SEED": "Close to {seed}: cosine.club similarity {0.81}.",
  "S-MENU-FROM-LABEL": "from: the label {Label}",
  "S-MENU-FROM-PERSON": "from: {Person}",
  "S-MENU-L-MANY": "its label ▸",
  "S-MENU-A-MANY": "its artist ▸",
  "S-MENU-NONE-LOADING": "(none listed yet)",
  "S-ROW-WHY-NEAREST": "nearest to {seed}",
  "S-ROW-WHY-UNMEASURED": "suggested from {seed}; similarity unavailable",
  "S-INS-WHY-NEAREST": "Nearest to {seed}. Rank {r} of {c}.",
  "S-INS-WHY-UNMEASURED": "Suggested from {seed}; cosine.club similarity unavailable. Rank {r} of {c}.",
  "S-INS-SCORE": "Digger score {0.79}: Digger's own order, merged from each origin's cosine.club list. The strongest candidate scores 1.00, even if the step hid it.",
  "S-INS-SCORE-FURTHER": "Digger score {0.79}: Digger's own order for the picks one step further, merged from the cosine.club lists it followed. Their strongest candidate scores 1.00, even if the step hid it.",
  "S-INS-SIMS-H": "cosine.club similarity to each seed",
  "S-INS-SIMS-BRANCH-H": "cosine.club similarity to the branch it followed",
  "S-INS-SIMS-FURTHER-H": "cosine.club similarity through the picks above",
  "S-HDR-HIDDEN": "Hidden from this step: {parts}.",
  "S-HDR-HIDDEN-OWNED": "{n} you own",
  "S-HDR-HIDDEN-SERVED": "{n} already in your Daily or Weekly",
  "S-HDR-HIDDEN-KEPT": "{n} you kept",
  "S-HDR-HIDDEN-PASSED": "{n} you passed",
  "S-HDR-HIDDEN-MUTED": "{n} by artists or labels you muted",
  "S-HDR-HIDDEN-KNOWN": "{n} you've dug from before",
  "S-SB-LIB-IDX-UNKNOWN": "your file · index check unavailable",
  "S-SB-LIB-IDX-IMP": "in your {Source} library · on cosine.club",
  "S-SB-LIB-NOTIDX-IMP": "in your {Source} library · not on cosine.club, so the dig would start from indexed tracks by {Artist} and say so",
  "S-SB-LIB-IDX-UNKNOWN-IMP": "in your {Source} library · index check unavailable",
  "S-ROW-KEPT-PREVIOUSLY": "★ kept previously",
  "S-ROW-PASSED-PREVIOUSLY": "✕ passed previously"
});


/** Expand a catalog template without interpreting any value as HTML. */
export function text(id, params = {}) {
  if (id == null) return '';
  if (!Object.hasOwn(strings, id)) throw new RangeError('Unknown string: ' + id);
  const values = {...params};
  if (values.sentence != null) {
    values['sentence with a lowercase first letter'] =
      String(values.sentence).replace(/^./u, (letter) => letter.toLocaleLowerCase());
  }
  if (values.digName != null) values['dig name'] = values.digName;
  if (values.chipText != null) values['chip text'] = values.chipText;
  if (values.artist != null) values.Artist ??= values.artist;
  if (values.title != null && values.artist != null) values['Artist - Title'] ??= values.artist + ' - ' + values.title;
  let template = strings[id];
  switch (id) {
    case 'S-SB-BTN': {
      const kind = values.kind === 'seed' || values.seeds?.some((seed) => seed.kind !== 'track') ? 'SEED' : 'TRACK';
      template = strings['S-SB-BTN-' + kind + (Number(values.n) === 1 ? '' : 'S')];
      break;
    }
    case 'S-SB-RECENT-META':
    case 'S-TR-META':
    case 'S-TR-SWITCH-ROW':
      if (!Number(values.k)) template = template.replace(' · {k} kept', '');
      if (Number(values.s) === 1) template = template.replace('{s} steps', '1 step');
      break;
    case 'S-TR-TIP':
      if (Number(values.c) === 1) template = template.replace('{c} tracks', '{c} track');
      break;
    case 'S-TR-YT-DONE':
    case 'S-T-YT-SENT':
      if (Number(values.n) === 1) template = template.replace('{n} tracks', '{n} track');
      break;
    case 'S-TR-YT-BEHIND':
      if (Number(values.n) === 1) template = "1 step isn't in it yet";
      break;
    case 'S-SET-LIB-SUM':
    case 'S-SET-LIB-SUM-MIX':
      if (Number(values.n) === 1) template = template.replace('{n} tracks', '{n} track');
      break;
    case 'S-HDR-META':
    case 'S-HDR-META-RETURN':
      if (Number(values.c) === 1) template = template.replace('{c} tracks', '{c} track');
      for (const key of ['k', 'p']) if (!Number(values[key])) {
        template = template.replace(' · {' + key + '} ' + (key === 'k' ? 'kept' : 'passed'), '');
      }
      break;
    case 'S-HDR-THESE-STEP':
    case 'S-HDR-THESE-DIG':
    case 'S-HDR-FROM-THESE':
    case 'S-TR-THESE':
      if (Number(values.k) === 1) template = template.replace('{k} keeps', '{k} keep');
      break;
    case 'S-HDR-SEED-N':
      if (Number(values.n) === 2) template = 'Sounds like {n} seeds: {s1}, {s2}';
      if (Number(values.n) >= 4) { template = strings['S-HDR-SEED-MORE']; values.k ??= Number(values.n) - 3; }
      break;
    case 'S-HDR-PROGRESS':
      if (values.further) template = strings['S-HDR-PROGRESS-FURTHER'];
      break;
    case 'S-TR-STUB':
      if (Number(values.n) === 1) template = '+ 1 more step';
      if (Number(values.k)) template += ' · ★{k}';
      break;
    case 'S-E-STOPPED':
      if (Number(values.i) === 0) template = strings['S-E-STOPPED-EMPTY'];
      break;
    case 'S-E-EMPTY':
      values['alt 1'] ??= 'sounds like its artist';
      values['alt 2'] ??= 'go one step further';
      break;
    case 'S-MAP-NOHUB':
      if (values.loading) template = strings['S-MAP-NOHUB-LOADING'];
      break;
    case 'S-N-SWAP-FILE':
      if (Number(values.n) === 1) template = strings['S-N-SWAP-FILE-1'];
      break;
    case 'S-N-LABEL-FEW':
      if (Number(values.m) === 1) template = 'Only {m} of the newest releases on {Label} is on cosine.club, so this step leans on it.';
      break;
  }
  return template.replace(/\{([^{}]+)\}/g, (token, key) =>
    Object.hasOwn(values, key) ? String(values[key] ?? '') : token);
}

/** Only ordinary HTTP(S) links, with no embedded credentials, are navigable. */
export function safeUrl(value) {
  if (typeof value !== 'string' || !value.trim() || /[\u0000-\u001f\u007f]/u.test(value)) return null;
  try {
    const url = new URL(value.trim(), document.baseURI);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

/** DOM construction with text nodes, explicit events, and checked URL attributes. */
export function h(tag, props = {}, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(props || {})) {
    if (value == null) continue;
    if (/^on/i.test(name)) {
      if (typeof value !== 'function') throw new TypeError('Event handlers must be functions');
      element.addEventListener(name.slice(2).toLowerCase(), value);
    } else if (['innerhtml', 'outerhtml', 'srcdoc'].includes(name.toLowerCase())) {
      throw new TypeError('HTML content is not allowed');
    } else if (['srcset', 'ping'].includes(name.toLowerCase())) {
      throw new TypeError('Multiple URL attributes are not supported');
    } else if (['href', 'src', 'action', 'formaction', 'poster', 'cite', 'background', 'longdesc', 'xlink:href'].includes(name.toLowerCase())) {
      const url = safeUrl(value);
      if (url) element.setAttribute(name.toLowerCase(), url);
    } else if (name === 'dataset') {
      for (const [key, item] of Object.entries(value)) element.dataset[key] = String(item);
    } else if (name === 'style') {
      if (!value || typeof value !== 'object') throw new TypeError('Styles must be an object');
      for (const [key, item] of Object.entries(value)) {
        if (key.startsWith('--') || key.includes('-')) element.style.setProperty(key, String(item));
        else element.style[key] = item;
      }
    } else if (name === 'textContent') {
      element.textContent = String(value);
    } else if (name === 'value') {
      element.value = value;
    } else if (['disabled', 'checked', 'hidden', 'selected', 'multiple', 'required', 'autofocus', 'readOnly'].includes(name)) {
      element[name] = !!value;
    } else {
      element.setAttribute(name === 'className' ? 'class' : name, String(value));
    }
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  if (element.getAttribute('target') === '_blank') {
    const rel = new Set((element.getAttribute('rel') || '').split(/\s+/).filter(Boolean));
    rel.delete('opener'); rel.add('noopener');
    element.setAttribute('rel', [...rel].join(' '));
  }
  return element;
}

/** Local 24-hour time; older dates use Product's "Thu 2 Oct 15:07" form. */
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function formatWhen(value, {now = new Date(), dateOnly = false} = {}) {
  if (value == null || value === '') return '';
  const date = new Date(value), today = new Date(now);
  if (!Number.isFinite(date.getTime())) return '';
  const day = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][date.getDay()] + ' ' + date.getDate() + ' ' +
    MONTHS[date.getMonth()];
  const time = String(date.getHours()).padStart(2, '0') + ':' + String(date.getMinutes()).padStart(2, '0');
  return dateOnly ? day : date.toDateString() === today.toDateString() ? time : day + ' ' + time;
}

/** "29 Sep", with the year when it isn't this one; '' for a missing or bad date. */
export function formatDay(value, {now = new Date()} = {}) {
  if (value == null || value === '') return '';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '';
  const day = date.getDate() + ' ' + MONTHS[date.getMonth()];
  return date.getFullYear() === new Date(now).getFullYear() ? day : day + ' ' + date.getFullYear();
}

/** A library row's importer for display: "plex" -> "Plex". */
export const sourceName = (source) => source === 'plex' ? 'Plex' : source ? source[0].toUpperCase() + source.slice(1) : '';

/**
 * Place an already-mounted popover inside the visible stage, flipping above
 * the viewport-coordinate anchor if needed. Returns its offset-parent position.
 */
export function clampPopover(popover, anchor, stage = document.getElementById('main'),
  {gap = 4, inset = 6, avoidAnchor = !!anchor} = {}) {
  if (!document.getElementById('main')?.contains(stage) || !stage?.contains(popover)) {
    throw new TypeError('Popovers must be mounted inside the stage');
  }
  const bounds = stage.getBoundingClientRect();
  const leftEdge = bounds.left + stage.clientLeft + inset;
  const topEdge = bounds.top + stage.clientTop + inset;
  const rightEdge = bounds.left + stage.clientLeft + stage.clientWidth - inset;
  const bottomEdge = bounds.top + stage.clientTop + stage.clientHeight - inset;
  popover.style.position = 'absolute';
  popover.style.boxSizing = 'border-box';
  popover.style.maxWidth = Math.max(0, rightEdge - leftEdge) + 'px';
  popover.style.maxHeight = Math.max(0, bottomEdge - topEdge) + 'px';
  popover.style.overflow = 'auto';
  let size = popover.getBoundingClientRect();
  const point = anchor?.getBoundingClientRect?.() || anchor || {left: leftEdge, top: topEdge};
  const x = point.left ?? point.x ?? leftEdge;
  const y = point.top ?? point.y ?? topEdge;
  const below = (point.bottom ?? y + (point.height || 0)) + gap;
  let flipped, top;
  if (avoidAnchor) {
    const aboveEdge = Math.max(topEdge, Math.min(y, bottomEdge));
    const belowEdge = Math.max(topEdge, Math.min(below, bottomEdge));
    const aboveRoom = aboveEdge - topEdge, belowRoom = bottomEdge - belowEdge;
    flipped = size.height > belowRoom && (size.height <= aboveRoom || aboveRoom > belowRoom);
    popover.style.maxHeight = (flipped ? aboveRoom : belowRoom) + 'px';
    size = popover.getBoundingClientRect();
    top = flipped ? aboveEdge - size.height : belowEdge;
  } else {
    flipped = below + size.height > bottomEdge;
    top = Math.max(topEdge, Math.min(flipped ? y - gap - size.height : below, bottomEdge - size.height));
  }
  const left = Math.max(leftEdge, Math.min(x, rightEdge - size.width));
  const parent = popover.offsetParent || stage;
  const origin = parent.getBoundingClientRect();
  const position = {left: left - origin.left - parent.clientLeft + parent.scrollLeft,
    top: top - origin.top - parent.clientTop + parent.scrollTop, width: size.width, height: size.height, flipped};
  popover.style.left = position.left + 'px';
  popover.style.top = position.top + 'px';
  return position;
}
