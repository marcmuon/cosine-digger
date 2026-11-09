# TODOS

## Lead ranking: count dig_from as a ranking boost
**Priority:** P3 | **Effort:** S (CC: S)
**Why:** `dig_from` is the strongest positive intent signal in the feedback loop — stronger than save. When heat scoring or ranking intelligence is added to `detect_leads()`, dig_from events should boost a lead's rank. Currently, dig_from marks a track as actioned but doesn't influence lead ordering.
**Context:** Identified by Codex adversarial review (2026-03-26). Depends on heat scoring being added first — the current plan intentionally ships without ranking (just appearances sort + explicit mute). When a formula is introduced, include `dig_from_count` alongside `save_count` and `pass_count`.
**Depends on:** Heat scoring or ranking formula in `detect_leads()` (not yet planned).
