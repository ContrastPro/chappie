# Immutable test fixtures

## `m4_10_recovery_proof_v6.sqlite3`

This is the byte-for-byte historical M4.10 fresh-process recovery-proof
database. It represents schema v6 after the zero-commission remediation and
before the Stage 4 managed SELL reached `PREPARED`.

SHA-256:

```text
1d3df2d7a07d236a7054319f52c64b72550a5bcb9818796cb376b2a11ac8ae90
```

`tests/test_m410_recovery_proof.py` copies this fixture to a per-test
temporary path before any adversarial mutation. Do not replace it with
`data/m4_10_testnet_smoke.sqlite3`: that protected runtime database represents
the later Stage 4 SELL recovery state and intentionally has SHA-256
`baeb4c9d459adc44876898b1df79a1cfe0f6a8c89f12c56024920543350d48ac`.
