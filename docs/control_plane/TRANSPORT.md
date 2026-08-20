# Local loopback transport v1

`chappie_control_plane_transport/1.0.0` wraps the read-only Control Plane for a future
local VS Code extension. It is an adapter: it owns HTTP lifecycle and JSON envelopes,
but no readiness, provider, Knowledge, RDE, or Trading authority.

## Scope and security

The server binds only to `127.0.0.1`, with stable default port `8765` and an explicit
trusted CLI port override. It never binds `0.0.0.0`, provides no remote or multi-user
access, adds no CORS headers, and exposes no mutation, setup, model, AI-generation, or
agent endpoints. It is not a remote API.

Start it explicitly with `chappie serve` (or `chappie serve --port 8765`). Human mode
prints its endpoint; `chappie serve --json` emits one JSON startup record without a
human banner, then serves. `Ctrl+C` performs orderly shutdown.

## Endpoints and handshake

All responses use `application/json; charset=utf-8` and a versioned envelope containing
transport/control-plane identities and digests, a per-request correlation ID, `OK` or
`ERROR`, data, and a safe bounded error. Unknown routes return JSON `404`; non-GET
methods return JSON `405`.

- `GET /v1/health` — minimal process/compatibility handshake; it does not call the
  Control Plane or probe AI.
- `GET /v1/status`
- `GET /v1/diagnostics`
- `GET /v1/ai/status`
- `GET /v1/capabilities`

## Future VS Code discovery

A future extension should probe `http://127.0.0.1:8765/v1/health`, launch the
venv-local `chappie serve` only when unavailable, wait with a bounded timeout, and
require compatible transport and Control Plane identities/digests before using routes.
The extension, WebSocket support, authentication, and remote access are intentionally
out of scope.
