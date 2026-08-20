# Local loopback transport v1

`chappie_control_plane_transport/1.2.0` wraps the read-only Control Plane for a
local VS Code extension. It is an adapter: it owns HTTP lifecycle and JSON envelopes,
but no readiness, provider, Knowledge, RDE, or Trading authority.

## Scope and security

The server binds only to `127.0.0.1`, with stable default port `8765` and an explicit
trusted CLI port override. It never binds `0.0.0.0`, provides no remote or multi-user
access, adds no CORS headers, and exposes no setup, model, agent, or remote endpoints.
It is not a remote API.

Start it explicitly with `chappie serve` (or `chappie serve --port 8765`). Human mode
prints its endpoint; `chappie serve --json` emits one JSON startup record without a
human banner, then serves. `Ctrl+C` performs orderly shutdown.

## Endpoints and handshake

All responses use `application/json; charset=utf-8` and a versioned envelope containing
transport/control-plane identities and digests, a per-request correlation ID, `OK` or
`ERROR`, data, and a safe bounded error. Unknown routes return JSON `404`; unsupported
methods/routes return JSON `405`.

- `GET /v1/health` — minimal process/compatibility handshake; it does not call the
  Control Plane or probe AI.
- `GET /v1/status`
- `GET /v1/diagnostics`
- `GET /v1/ai/status`
- `GET /v1/capabilities`
- `POST /v1/project/qa` — one bounded `{ "question": "..." }` Project QA query.
  HTTP POST expresses query execution only: the application operation remains read-only.
  It uses canonical Knowledge retrieval and validated `PROJECT_QA_V1`, returns only a
  summary, authoritative record IDs/titles, explicit unknowns, safe provider/model
  metadata, and validation status. It never returns provider reasoning or raw output.

The Project QA endpoint rejects empty, oversized, malformed, and unknown-field bodies
with a typed `400` envelope. Provider, Knowledge, and timeout failures are bounded safe
errors. It does not accept workspace files, selected text, repository diffs, conversation
history, setup/model controls, RDE execution, or Trading actions. Conversation persistence
is intentionally deferred.

Phase 3 permits an explicit optional `workspace_context` object on this request. The strict
`workspace_context/1.0.0` schema accepts only workspace-relative active-file metadata,
bounded selection or excerpt text, line ranges, and safe Git branch/commit/dirty metadata.
It includes a deterministic digest; absolute paths, traversal, malformed ranges, unknown
fields, oversized text, and digest mismatches are rejected with `400`. Workspace context
remains user-supplied provenance, separate from canonical Knowledge citations.

## Future VS Code discovery

A future extension should probe `http://127.0.0.1:8765/v1/health`, launch the
venv-local `chappie serve` only when unavailable, wait with a bounded timeout, and
require compatible transport and Control Plane identities/digests before using routes.
The extension, WebSocket support, authentication, and remote access are intentionally
out of scope.
