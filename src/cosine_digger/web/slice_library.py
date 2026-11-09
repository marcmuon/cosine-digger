"""Library label adapters, independent of routes and either job runtime."""

from contextlib import closing

from .. import storage, taste


def label_overview(conn) -> dict:
    """Read the library and latest label job without starting provider work."""
    total = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    missing = conn.execute(f"SELECT COUNT(*) FROM tracks WHERE {taste._MISSING_LABEL}").fetchone()[0]
    pairs, _ = taste._label_candidates(conn)
    active = conn.execute(
        "SELECT id FROM slice_jobs WHERE kind='label_enrichment' AND state='running'"
    ).fetchone()
    last = conn.execute(
        "SELECT id FROM slice_jobs WHERE kind='label_enrichment' ORDER BY started_at DESC,id DESC LIMIT 1"
    ).fetchone()
    return {"total_tracks": total, "missing_tracks": missing, "eligible_pairs": len(pairs),
            "active_job_id": active[0] if active else None,
            "last_job": storage.get_slice_job(conn, last[0]) if last else None, "budget_default": 100,
            "sources": storage.library_sources(conn)}


def run_label_enrichment(*, job_id, get_config, client_factory, cancelled, on_progress) -> dict:
    """Run S8's committed album work; S7 owns journaling and terminal state.

    client_factory follows chain.make_rate_limited_client(budget, http_cache,
    token) -> (client, tracker). It is lazy, worker-owned and closed here.
    The current config's token wins over environment or captured settings.
    """
    config = get_config()
    with closing(storage.connect(config.db_path)) as conn:
        job = storage.get_slice_job(conn, job_id)
    if job["kind"] != "label_enrichment" or job["state"] != "running":
        raise ValueError("label enrichment requires its own running job")
    work = dict(job["progress"].get("work", {}))
    budget = work.get("budget", job["progress"].get("budget", 100))
    return taste.enrich_from_discogs(
        config.db_path, token=config.discogs_token, client=client_factory, budget=budget,
        cancelled=cancelled, on_progress=on_progress, resume=work | {"job_id": job_id}, quiet=True,
    )
