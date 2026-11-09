---
name: dig
description: "Turn a few favorite tracks or albums into a private YouTube Music playlist of unfamiliar music, each pick with a one-line why. Use for music discovery: 'find music like …', 'make a playlist from these albums', /dig."
---

# /dig

One call does the whole dig: resolve the seeds, find neighbours on cosine.club, write the private playlist, verify it. Pass the user's seeds through as written; the engine resolves them.

```sh
cosine-digger quick "Artist - Track" "Artist - Album" "https://www.discogs.com/release/…" --json \
  [--playlist "NAME"] [--limit 25] [--deep]
```

Inside a source checkout, run it as `uv run cosine-digger`. Seeds: 2-10 of `Artist - Track`, `Artist - Album`, or a Discogs / YouTube / cosine.club URL. The fast path answers in seconds. `--deep` adds one hop through labels and artists (about 90 s); use it when the user asks for deeper, stranger or label-driven digging.

Read the JSON: `seeds[]` (`raw`, `ok`, `notes`), `picks[]` (`artist`, `title`, `why`, `youtube_url`), `playlist_url`, `export` (`verified`, `added`, `missing`, `failed`), `export_error`, `error`, `timings`.

Reply with:
1. The playlist link first, and whether `export.verified` is true.
2. The picks as a numbered list: `Artist - Title: why`.
3. Any seed with `ok: false`, its note, and the spelling or URL that would fix it.
4. On `export_error` about the login: tell the user to run `cosine-digger youtube login` once and paste their YouTube Music request headers. The picks still stand, so list them anyway.

The dig is done when the user has the playlist URL (or the exact export error) and every pick's why.
