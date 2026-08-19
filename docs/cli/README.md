# Chappie CLI (Phase 1)

`chappie` is the product control plane. It is intentionally separate from the
specialized `python -m app.tools...` development and research commands.

## Setup (Phase 2)

Bootstrap a source checkout with `./setup.sh` on macOS/Linux or `./setup.ps1`
on Windows. The scripts only locate compatible Python 3.13+, create/reuse
`.venv`, install the checkout, and delegate to the Python setup engine. They do
not use `sudo`, change shell profiles, or install system packages.

### Windows clean-install procedure

Prerequisites are Git and Python 3.13 or newer. Clone the repository, change to
the checkout, then run:

```powershell
.\setup.ps1
```

The script reuses `.venv` on later runs, installs the checkout into that venv,
asks for explicit consent before any external Ollama/model work, then invokes
the canonical `chappie setup --yes` flow. The CLI uses its hardware/catalog
logic to show recommendations and prompts for `LIGHT`, `BALANCED`, `ADVANCED`,
or `SKIP`. It runs the official interactive Ollama installer and model download
only after consent, then reports its own verified setup result. Declining
consent leaves safe bootstrap usable but external AI setup incomplete.

Use the venv-local command until you intentionally add it to PATH:

```powershell
.\.venv\Scripts\chappie.exe status
.\.venv\Scripts\chappie.exe doctor
.\.venv\Scripts\chappie.exe ai status
```

`chappie setup --dry-run` is offline and makes no changes. `chappie setup --yes`
may perform only explicitly classified SAFE project-local actions. Provider
installation and model downloads are EXTERNAL actions and require explicit
consent. `--developer` adds
optional metadata-based developer preparation; editor tooling is never required.

### Windows Ollama acceptance policy

Windows uses the official interactive `OllamaSetup.exe` from
`https://ollama.com/download/OllamaSetup.exe`. The current official policy is
per-user installation without administrator rights; Chappie uses no undocumented
silent flags. Its provenance policy is `OFFICIAL_SOURCE_TLS_ONLY` because no
official checksum is currently consumed by this executor. Model pulls use
argument-vector `ollama pull <catalog-model>` only after explicit external consent.

```text
chappie --help
chappie status
chappie status --json
chappie doctor
chappie setup --plan
chappie ai status
chappie knowledge verify
```

The setup experience is clone → bootstrap → canonical `chappie setup` →
`chappie status` → `chappie`. `status` has stable exit
codes: `0` READY, `1` setup required/degraded, `2` unsupported installation,
and `3` reserved for internal diagnostic failure.

Development-only expensive tests, benchmarks, and research diagnostics belong
under `chappie dev ...`; they are never run by status, doctor, or setup planning.
Local configuration is non-secret and portable. In a checkout it lives under
ignored `data/config/chappie.json`; future packaged installs use a user-local
Chappie state directory through `ChappiePaths`.
