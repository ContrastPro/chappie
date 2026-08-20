# Cross-platform setup behavior contract

**Identity:** `chappie_platform_setup/1.0.0`

This is the platform-neutral contract for `chappie setup`. It defines observable
setup behavior, not the mechanism used by a particular operating system. The
shared CLI and `SetupEngineV1` own this contract; platform adapters supply only
platform mechanics.

## Lifecycle

```text
CHECK → DECIDE → REQUEST AUTHORIZATION ONLY IF REQUIRED → APPLY → VERIFY → BIND → SMOKE → READY
```

`CHECK` is bounded and read-only. `DECIDE` resolves existing valid state before
asking the user anything. A setup run must never ask for authorization or a
choice when verified state already determines that no action or decision is
needed.

Discovery alone is not readiness:

- an executable being present is not provider runtime readiness;
- a successful pull command is not exact model verification; and
- a Knowledge file existing is not Knowledge readiness.

`READY` therefore requires the applicable verified postconditions: provider
runtime readiness, exact model verification, canonical Knowledge verification,
atomic configuration binding, Chappie AI structured smoke, and Project QA smoke.

## Three-step workstation onboarding acceptance criterion

Every production-supported desktop platform must prove this normal-workstation
experience:

```text
clone repository → run one platform bootstrap command → open the repository in normal VS Code
```

On macOS and Linux the sole bootstrap command is `./setup.sh`; on Windows it is
`./setup.ps1`. A supported workstation with VS Code already installed must be
ready to work with Chappie after that command: the project environment, required
runtime dependencies, canonical Knowledge, configured AI, and compatible local
Chappie VS Code extension are ready. Opening the repository in normal VS Code
then activates Chappie and lets the extension start or connect to its owned
Control Plane as designed.

Normal onboarding must not require npm, manual VSIX installation, manual Ollama
start, manual model pull, a manual Chappie server command, Extension Development
Host/F5, or a Marketplace publication. Setup may prompt only for genuine choices
or explicit external authorization required by this contract.

For headless/server use, the equivalent criterion is:

```text
clone → one platform bootstrap command → READY
```

VS Code integration is optional or not applicable there and must never block
core readiness. A future desktop OS is not production-supported until it proves
the same three-step onboarding acceptance criterion.

### Development checkout rule

Canonical Knowledge is built only from a clean committed source snapshot. A
development checkout with tracked or untracked source changes may reuse a
verified store matching its committed snapshot, but it must not rebuild one from
dirty sources. Setup reports `KNOWLEDGE_BUILD_BLOCKED_DIRTY_REPOSITORY` with
commit/stash remediation and does not report READY. Gitignored generated local
state is not source dirtiness. `--developer` does not bypass this provenance
rule.

## State before prompt

The required order on every supported platform is:

```text
inspect current state → find a missing postcondition or genuine ambiguity → prompt only if necessary
```

The inverse order—prompting before discovering that the provider, model, or
configuration was already ready—is forbidden. Platform adapters expose bounded
facts; they do not implement independent prompt policy.

## Idempotency and decisions

| Current verified state | Required setup behavior |
| --- | --- |
| Valid `.venv` | Reuse it; do not ask to recreate it. |
| Dependencies satisfied | Reuse them. |
| Current Knowledge snapshot | Reuse it; do not rebuild it. |
| Provider installed | Do not run its installer. |
| Provider runtime READY | Do not start it. |
| Selected exact model verified | Do not pull it. |
| Valid bound Chappie AI config | Reuse its provider, profile, and model; do not prompt for a profile. |
| No config and exactly one supported installed catalog model | Deterministically adopt its profile; do not prompt. |
| No config and multiple supported catalog models | Treat this as real ambiguity and ask the user. |
| No supported catalog model | Recommend/select a profile, then obtain external authorization before a pull. |
| Explicit `--ai-profile` | Treat as intentional reconfiguration; it overrides automatic adoption. |
| No external work in the current plan | Never ask for external authorization. |
| External provider install or model pull remains | Obtain explicit authorization before `APPLY`. |

An existing valid config has continuity priority on an ordinary rerun. A changed
hardware recommendation must not silently replace it; for example,
`BALANCED/qwen3:8b` remains selected even if a later recommender prefers
`LIGHT/qwen3:4b`. A missing configured model is not ready state: remediation
still requires the appropriate decision and authorization.

## Consent

Consent is **action-scoped**. It is neither permanent nor machine-global, and
it is not implied by `--yes` or inferred from a previous run. An already
installed `qwen3:8b` needs no consent; a requested new `qwen3:14b` needs fresh
consent before it is pulled. A missing provider likewise needs consent before
installation. In non-interactive mode, externally required work without
authorization fails closed; a fully ready run succeeds without authorization.

Real user decisions are limited to genuine ambiguity, explicit profile
reconfiguration, and external installation approval. Asking again for an
already valid `BALANCED` configuration, a uniquely installed `qwen3:8b`, or
permission when no download/install will occur is a redundant prompt and a
contract violation.

## Responsibilities

Platform adapters are responsible for provider installation mechanics,
executable discovery, runtime start mechanics, hardware inspection, and
platform-specific paths. They are not responsible for prompt policy, consent
semantics, profile adoption, READY aggregation, Knowledge semantics, or AI
smoke semantics. Those remain shared setup behavior.

No new operating system may implement setup behavior from scratch. It must add
the required adapter(s), plug into the shared `SetupEngineV1`, satisfy this
contract, and pass the acceptance scenarios below. A platform divergence needs
an explicit contract decision.

## Platform parity

Windows is the first proven reference implementation. Its accepted clean flow
is clone → `setup.ps1` → `.venv` → project install → Knowledge → provider/model
→ config → AI smoke → Project QA → READY. Its accepted rerun revalidates an
existing config/provider/model, avoids profile and external-consent prompts,
does not reinstall or repull, runs the smokes, then reaches READY. Windows
paths and executables are implementation details, not cross-platform rules.

| Behavior | Windows | macOS | Required contract |
| --- | --- | --- | --- |
| Detect existing provider | Implemented | Implemented | Bounded adapter check |
| Detect runtime readiness | Implemented | Implemented | READY is verified, not inferred |
| Detect exact installed catalog model | Implemented | Implemented | Exact identity only |
| Reuse valid bound config | Implemented | Shared CLI behavior | No profile prompt |
| Adopt one installed model | Implemented | Shared CLI behavior | Deterministic, no prompt |
| Multiple models | Implemented | Shared CLI behavior | Prompt for a real decision |
| Avoid redundant external consent | Implemented | Shared CLI behavior | Consent only for required action |
| Atomic config binding | Implemented | Shared engine behavior | Bind only after verification |
| AI and Project QA smoke | Implemented | Adapter-wired | Required before READY |
| Idempotent rerun/status/doctor | Implemented | Shared status behavior | Revalidate without side effects |

For the current Mac acceptance case—Ollama and `qwen3:8b` installed with no
bound Chappie config—the required path is: detect provider/model → uniquely map
`qwen3:8b` to `BALANCED` → adopt it without profile or external-consent prompt
→ verify → bind → smoke → READY. Remaining macOS acceptance work is real
end-to-end proof of this existing shared path, not a separate behavior design.

## Acceptance scenarios

Each supported platform must provide equivalent deterministic coverage for:

- `CLEAN_MACHINE`
- `EXISTING_PROVIDER`
- `EXISTING_PROVIDER_AND_MODEL`
- `FULLY_READY_RERUN`
- `MULTIPLE_EXISTING_MODELS`
- `MODEL_MISSING`
- `PROVIDER_DOWN`
- `PROFILE_CHANGE`
- `DECLINED_EXTERNAL_CONSENT`
- `INTERRUPTED_SETUP_RESUME`
- `STALE_CONFIG`
- `STALE_KNOWLEDGE`

Shared tests cover behavior such as config reuse, unique-model adoption,
ambiguity, explicit-profile override, and action-scoped consent. Adapter tests
cover only mechanics such as Windows or macOS discovery, installation, and
launch. This split should grow incrementally; it does not require a large test
refactor to be useful.

## Future Linux adapter checklist

Before Linux is supported, implement and prove:

1. official-provider installation and executable discovery without weakening
   provenance or consent;
2. bounded local runtime readiness and non-blocking start behavior;
3. exact catalog-model verification and argv-only model operations;
4. hardware/path inspection appropriate to Linux; and
5. every acceptance scenario and shared behavior in this contract.

Linux support is complete only when it has behavioral parity, not merely an
installer.

## Optional workstation integration

Core Chappie setup is complete without VS Code.  After the core Knowledge, AI,
and Project QA postconditions are verified, a supported workstation may perform
one optional local integration: install the bundled Chappie VS Code extension.
Its absence, a missing VS Code CLI, or a local extension-install failure must
never turn a verified core setup into a non-READY result.

The distributable is a versioned repository artifact at
`tools/vscode-chappie/dist/chappie-vscode-<version>.vsix`.  Before any VS Code
call, setup verifies its SHA-256 against the adjacent `SHA256SUMS` manifest.
The extension is installed only through argv-safe local CLI execution:

```text
code --install-extension <verified-local-vsix> --force
```

No marketplace lookup, Node/npm execution, shell command, model action, or
external authorization is involved in normal workstation installation. Node is
developer-only tooling used to rebuild the checked-in VSIX. Setup does not
reinstall an equal or newer compatible local extension and never silently
downgrades a newer one. Windows resolves `code.cmd`/`code.exe` before `code`;
other supported platforms resolve `code`.
