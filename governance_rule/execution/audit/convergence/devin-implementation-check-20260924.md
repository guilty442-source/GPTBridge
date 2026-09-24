
## Follow-up — Investment-mobile Convergence

- Worker v2.0.0 restructure: trading/ engine layer live; DOMAINS=14 (live-trading added with phase-locked-dispatch boundary)
- Fixed market stream state leak: source cancelled mid-failure no longer stuck at degraded → DISCONNECTED on task exit (commit ea7cacdf)
- LIVE mode contract verified: gate-level set_mode → LIVE_PRECONDITIONS_UNMET (precondition check); dispatch-level → LIVE_PHASE_LOCKED (phase-gate check) — both correct per layer
- Tests: investment-mobile 184/184, ai-assistant 16/16
- Main system: healthy after external-process crash (WER heap-corruption 0xc0000374, two copies of _sovereign_native.pyd loaded — fixed in 43df7e45, faulthandler armed)
