# Stage 4 managed SELL recovery disposition design

Status: **PROPOSED — ADR-067 is not accepted**.

This design applies only to the existing durable graph:

- one consumed Stage 4 managed-SELL authority;
- its one deterministic SELL command and client order ID;
- one `PREPARED` SELL order for exactly `0.00016 BTC`;
- zero SELL submission attempts, no `SUBMITTING` event and no exchange order ID.

It authorizes no production migration, exchange read, continuation, or submission.

## State machine

The only disposition sequence is:

```text
PREPARED
  -> SELL_RECOVERY_REQUIRED
       -> RECOVERY_CONTINUATION_ALLOWED
       -> DISPOSITION_REQUIRED
```

`SELL_RECOVERY_REQUIRED` is append-only and idempotent for the authority. Exactly one of the two
ordinal-2 decisions may follow. Neither decision updates or deletes the authority, command, order,
reservation, lifecycle event, or predecessor Portfolio. There is no automatic transition and no
generic `PREPARED -> SUBMITTING` path.

`RECOVERY_CONTINUATION_ALLOWED` is audit truth, not submission authority. A future implementation may
return one non-copyable, non-serializable, process-local recovery claim only from the transaction that
commits that decision. The durable event cannot recreate the claim. If the process dies after the
decision commit but before `SUBMITTING`, availability is sacrificed: the claim is lost, the order
remains recovery-only, and no replay or replacement is permitted.

## Immutable economic identity and reservation

Continuation means continuing the existing economic action. These values must match the consumed
authority, PREPARED order, canonical command payload and deterministic derivation:

- authority ID;
- durable command key;
- client order ID;
- economic-slot key;
- account, execution root and session;
- side `SELL`, symbol `BTCUSDT`, type `MARKET`;
- exact quantity `0.00016 BTC`;
- predecessor Portfolio ID, version and digest;
- managed-BTC ownership derived from the settled source BUY.

The PREPARED order remains the sole logical reservation of that managed inventory. Recovery does not
release, duplicate, round, resize or recreate it. A new authority, command, client ID, price
authorization, quantity, slot or reservation is prohibited even if current market conditions differ.

## Required continuation proof

Every condition below is mandatory in one fresh recovery review. Any mismatch selects
`DISPOSITION_REQUIRED`:

1. The exact consumed authority, event, command, PREPARED order and client ID pass canonical digest,
   foreign-key and deterministic-identity validation.
2. No `SUBMITTING` event, submission-attempt record, exchange order ID or later lifecycle fact exists.
3. The Portfolio ID/version/digest and exact managed BTC position equal the authority predecessor;
   the settled BUY remains the sole managed source and external BTC remains unmanaged.
4. Session submission count/economics, execution root, account fingerprint and configuration
   fingerprint are unchanged.
5. Reconciliation is current and `SAFE`, settlement is globally `SYNCHRONIZED`, and there are no
   pending or unresolved blocked settlements.
6. The durable kill switch remains `ACTIVE`; control and reconciliation generations equal those bound
   to the authority.
7. Current effective MARKET rules still accept the exact unchanged quantity and match the reviewed
   rules contract. There is no rounding or substitute rules envelope.
8. Financial and exchange Risk re-evaluate the same command and ownership scope under the original
   maximum envelope. Current price may be observation input, but cannot create a new command or price
   authority; failure to approve selects disposition.
9. Bounded authenticated read-only reconciliation of the exact account/client identity finds no order,
   open order, trade or other managed history for the SELL identity, and its sanitized observation
   digest is bound to the decision.

Binance `NOT_FOUND` is not strong negative proof by itself. Unknown, incomplete, contradictory or
unavailable exchange state always selects `DISPOSITION_REQUIRED`; it never authorizes submission.
Because absence is a manual safety judgment, continuation requires an audited operator decision in
addition to the bounded observations.

## Authorization and final gate

Recovery uses only the exact token `STAGE4_SELL_RECOVERY_AUTHORIZED=YES`. It is distinct from Phase 3
BUY and normal managed-SELL authorization. Credentials, account access or possession of the original
SELL token cannot substitute for it. Authorization and evidence-collision checks precede writable
database acquisition, migration, credential inspection and transport construction.

A future final recovery gate must atomically reread and compare:

- recovery decision and process-local recovery claim;
- authority, command, client identity and economic slot;
- Portfolio/ownership and session predecessor;
- ACTIVE control and current SAFE reconciliation;
- synchronized settlement;
- current rules, Risk, configuration and bounded exchange-absence observation.

Only that gate may consume the recovery claim and call a dedicated persist-before-side-effect recovery
submission primitive for the existing command. `SUBMITTING` and the sole attempt must commit before
the POST. The normal SELL claim, ordinary submission capability and direct persistence calls remain
invalid. No retry, replacement, cancellation, fallback or LIVE path is introduced.

## Crash, restart and ambiguity

- Before the recovery-decision commit, interruption rolls back and leaves `SELL_RECOVERY_REQUIRED`.
- After a `DISPOSITION_REQUIRED` commit, continuation is permanently unavailable.
- After a continuation-decision commit, loss of its process-local claim does not permit reconstruction.
- After `SUBMITTING`, every restart is reconciliation-only; transport outcome is never inferred.
- Unknown exchange state before or after submission blocks. It cannot be converted to absence.

## Sanitized evidence

Recovery evidence is exclusive, append-only in meaning and written only after owned resources close.
It may contain authority ID, command key, client ID, economic slot, exact quantity, reason taxonomy,
decision, snapshot/rules/Risk/configuration digests, Portfolio/session/reconciliation/control lineage,
request counts and timestamps. It must not contain credentials, signatures, headers, private request
parameters, raw exchange responses, environment values or secret-derived material. `PASS` or
`CONTINUATION_ALLOWED` is written last and only after durable verification.

## Implementation boundary

ADR-067 remains proposed. Implementation requires a separate offline task and adversarial review.
Production recovery, authenticated reads, claim issuance and SELL continuation remain unauthorized.
Stage 4 stays blocked and Stage 5 has not started.
