---
name: "taste-backup"
description: "Backup or restore the local taste DB (SQLite + manifest) to ~/.cosine-taste/. Use when you want to snapshot your taste DB before experimenting, or restore a previous version. Run with 'backup' to save or 'restore' to recover."
allowed-tools: "Bash, Read, Glob"
metadata:
  short-description: "Backup/restore taste DB snapshots"
---

# taste-backup — Snapshot and Restore Your Taste DB

## What this does

Manages local snapshots of your taste DB and manifest in `~/.cosine-taste/` (a local git repo, never pushed). Your taste DB contains personal library data and Discogs enrichment cache — this skill lets you version it independently from the cosine-digger repo.

## Usage

**Backup** (default): Copy current DB + manifest to `~/.cosine-taste/` and commit.
```
/taste-backup
/taste-backup backup
/taste-backup backup "before adding new folders"
```

**Restore**: Copy the latest (or a specific) snapshot back to the repo.
```
/taste-backup restore
/taste-backup restore <commit-hash>
```

**List**: Show available snapshots.
```
/taste-backup list
```

## Workflow

### Backup

1. Ensure `~/.cosine-taste/` exists and is a git repo (create if not).
2. Copy `sample_taste.db` and `sample_manifest.jsonl` from the repo root to `~/.cosine-taste/`.
3. Stage and commit with a timestamped message (or the user's message if provided).
4. Report the commit hash and file sizes.

### Restore

1. If no commit hash given, use the latest commit in `~/.cosine-taste/`.
2. If a commit hash is given, checkout those files at that revision.
3. Copy `taste.db` and `manifest.jsonl` from `~/.cosine-taste/` back to the repo root as `sample_taste.db` and `sample_manifest.jsonl`.
4. Report what was restored.

### List

1. Run `git log --oneline` in `~/.cosine-taste/` to show available snapshots.

## File mapping

| Repo file | Backup file |
|-----------|-------------|
| `sample_taste.db` | `~/.cosine-taste/taste.db` |
| `sample_manifest.jsonl` | `~/.cosine-taste/manifest.jsonl` |

## Safety

- Never pushes to any remote
- Never modifies the cosine-digger repo's git history
- The backup repo is local-only at `~/.cosine-taste/`
- If the backup repo doesn't exist, the skill creates and initializes it
