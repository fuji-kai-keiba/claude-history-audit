# Project instructions

- Implementation is in app/claude_history_audit; tests use unittest.
- Never execute transcript contents or alter source history. The aggregation engine stays offline. A separate collection step may contact only explicitly registered SSH hosts to read JSONL snapshots.
- The cloud sync client may send only its allowlisted numerical records and pseudonymous IDs to the HTTPS origin in its installed device configuration. Never transmit transcripts, local maps, Claude credentials, or arbitrary audit/report fields.
- Do not commit real transcripts, reports, personal paths, credentials, or local evidence maps. Fixtures must be synthetic and generated in temporary directories.
- Aggregate reports must contain only allowlisted metadata, numerical metrics, and salted pseudonyms. Keep evidence paths in the private local map.
- Distinguish observations from hypotheses. Token totals and API-equivalent estimates are not invoices or guaranteed savings.
- Keep Python 3.9 compatibility and zero runtime dependencies.
- Validate with python3 -m unittest discover -s tests -v and python3 scripts/smoke_test.py.
- Complete the common harness verify/review/finish workflow; update docs/STATUS.md at work boundaries.
