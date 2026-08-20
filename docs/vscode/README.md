# Chappie VS Code Extension v1

The local Chappie extension lives in [`tools/vscode-chappie/`](../../tools/vscode-chappie/).
It is a native VS Code TypeScript extension that consumes the local Control Plane; it
does not import Python application internals, Ollama, Knowledge storage, or Trading code.

## Phase 1

On activation it probes `http://127.0.0.1:8765/v1/health`, verifies the frozen transport
and Control Plane identities/digests, and shows `CONNECTED`, `DISCONNECTED`,
`INCOMPATIBLE`, or `ERROR`. The Chappie Activity Bar view presents connection, system,
AI, Knowledge, and implemented-capability status. `Chappie: Refresh` refreshes only
read-only endpoints; `Chappie: Show Doctor` writes bounded diagnostics to the Chappie
Output channel.

When a workspace contains all three bounded Chappie markers — `pyproject.toml`, `app/`,
and `app/cli.py` — activation probes the local Control Plane. If it is unavailable and
the workspace-local virtualenv executable exists, the extension can start
`.venv/bin/chappie serve --port <configured-port>` (or the Windows equivalent) with
argv-safe spawning. It never starts a process for another workspace, a missing virtualenv,
or an incompatible server. `Chappie: Stop Local Control Plane` stops only that
extension-owned child; an already-running compatible server is never owned or killed.

`Chappie: Start Local Control Plane` is the explicit equivalent. If setup is missing, the
extension reports that the Chappie development environment is not set up; it does not
create a virtualenv or run setup itself.

Settings are `chappie.controlPlane.port` (default `8765`), `chappie.autoConnect`
(default `true`), and `chappie.autoStartControlPlane` (default `true`, recognized
Chappie workspaces only). There is intentionally no host setting: the extension only uses
`127.0.0.1` and no CORS, remote API, credentials, model control, or setup logic exists.

## Development

```bash
cd tools/vscode-chappie
npm install
npm run compile
npm test
npm run lint
npm run package
```

`npm run package` creates the ignored local artifact `chappie-local.vsix`. Install it
without the Marketplace using:

```bash
code --install-extension chappie-local.vsix --force
```

Then open the normal Chappie repository in VS Code. The extension does not require F5 or
an Extension Development Host for normal use. The later `chappie setup --developer`
backlog should detect or remediate Node LTS, npm, VS Code, and extension prerequisites.

## Phase 2: grounded Project Chat

The Chappie Activity Bar includes a native Webview chat view. It submits an explicit
question only to local `POST /v1/project/qa`; it never talks to Ollama directly. The
Control Plane performs canonical Knowledge retrieval and validated `PROJECT_QA_V1`, then
returns an answer summary, authoritative source record IDs/titles, explicit unknowns, and
safe configured-model metadata. This is not generic chat.

The view uses an extension-memory-only history capped at 20 turns. It has no workspace or
file context, no code-editing/action authority, no streaming, and no conversation store.
Scripts are limited to a nonce-protected local Webview script; all question, answer,
source, and unknown text is escaped before rendering. Phase 3 may add a separately scoped
workspace-context proposal.

## Degraded local setup

Control Plane connectivity is independent from optional local AI and Knowledge readiness.
The sidebar keeps `Connection: CONNECTED` when those dependencies are unavailable, while
showing the system, AI, Knowledge, and a compact capability summary. **Chappie: Show
Capabilities** writes the detailed read-only list to the Chappie Output channel.

When Project QA is unavailable, the chat view explains whether AI, Knowledge, or both need
configuration before accepting a question. **Chappie: Show Setup Instructions** is
read-only: on macOS it explicitly states that production local-Ollama setup is not yet
implemented and points to `.venv/bin/chappie setup --plan` for safe local setup planning.
The extension never installs Python, Ollama, or a model itself.

`ROBOT_ICON_LICENSE_PENDING`: the extension retains its current local Chappie icon. No
third-party robot asset is included until a redistributable source and its attribution
requirements are verified.

## Phase 3: bounded workspace context

Workspace context is opt-in for each question. The extension may send only the active
text document's normalized workspace-relative path, language, a selection (preferred) or
a cursor-centred excerpt, and optional safe Git metadata. Selection/excerpt text is capped
at 12,000 characters and carries deterministic line ranges plus a SHA-256 context digest.
Context is request-scoped and never becomes canonical Knowledge or disk-persisted chat
history. `.env`, `.env.*`, credentials, secrets, PEM, and key files are withheld.

There is no repository scanning, arbitrary-file loading, Git diff content, terminal
context, semantic search, test execution, code editing, patches, or approval workflow.
Those are future Workspace Intelligence work.

When **Include Git metadata** is selected, the extension uses only bounded argv-safe local
Git calls for branch, `HEAD`, and porcelain status. It sends only branch (or `null` for a
detached HEAD), commit hash, and dirty boolean. Git absence, timeout, or failure is
non-fatal and omits this context; no diff, remote URL, changed path, history, or
credential is exposed.
