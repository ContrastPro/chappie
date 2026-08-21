# Chappie Workbench Design Specification

Status: approved UI implementation baseline
Date: 2026-08-21
Internal protocol: `chappie_workbench/1.3.0`

## Purpose and principles

Chappie Workbench is the editor-area, project-aware conversation surface for the local Chappie Control Plane. It follows five principles: IDE-first, information-dense, context-aware, real-time, and modular. It must present only capabilities and state that exist; visual similarity to future Agents or Market Intelligence concepts never authorizes fake data or controls.

## Canonical layout

The product uses the native VS Code Activity Bar and Side Bar plus one reusable editor-area Workbench panel. The panel contains a document-style conversation and an optional Inspector. At widths below 900 px the Inspector becomes an overlay drawer; below 620 px secondary header metadata is reduced. The conversation must retain the largest useful width and the composer stays available at the bottom.

The native Chappie Side Bar is the navigation and operational surface. It exposes real connection/system/AI/Knowledge/capability state, New Chat, Open Workbench, Project QA, Health Check, and Diagnostics. It must not host a second composer. Session history remains ephemeral and is not represented as durable history until a real persistence contract exists.

## Workbench header and conversation

The header shows `Chappie Workbench`, Beta, Ready/Needs attention/Disconnected, configured model, Knowledge state, the canonical setup/repair/reconnect action when needed, Inspector access on narrow layouts, and New Chat. Debug payloads and speculative mode selectors are forbidden.

Turns render as a document: user question, Chappie answer, safe Markdown, code blocks, source disclosure, and answer actions. New Chat clears settled turns but never discards an in-flight request. Retry reuses the original bounded question and context through the existing Project QA path. Good answer / Not good are session-local feedback only and make no durability claim.

## Composer and context

The composer auto-grows, submits with Ctrl/Cmd+Enter, and supports explicit File, Selection, and Git context through the existing bounded workspace collector. Context is opt-in, previewed without absolute paths or sensitive content, and never broadens the collector’s security boundary. Web search, Deep Research, autonomous Agent mode, attachments, and cancellation remain unavailable until backed by approved APIs.

## Inspector

The local draft limit is 50,000 characters, while the explicit Project QA request limit is 12,000 characters. The latter preserves headroom for supported 16K-context local models and the independently bounded 12,000-character workspace context; an oversized draft stays editable but is never silently truncated or sent. File opens the workspace file picker, Selection captures a non-empty active editor selection, and Git captures bounded local repository metadata.

The Inspector is a floating secondary surface at wide and medium widths: it is rounded like the composer, uses the same theme-aware elevated background and subtle border, and has visible right/top/bottom outer spacing. Its content scrolls independently. Closing it applies an explicit scoped hidden-display rule and collapses the grid slot instead of leaving an empty right margin or reflowing the Inspector below the conversation. At narrow widths it remains the existing responsive drawer, inset from the Webview edges; it must not collide with a native Codex panel. This presentation change does not alter Inspector data, selection, lifecycle, or authority.

The Inspector has four tabs:

- Reasoning: bounded response metadata only—Project QA validation status, authority, source count, and measured host-side duration. It never displays chain-of-thought.
- Sources: the exact structured Knowledge records returned by Project QA.
- Activity: actual request start/completion/failure lifecycle derived from the host-owned turn state.
- Codex: an explicit unavailable statement until a supported integration exists.

Inspector selection and disclosure state are UI-local. Handler failures or presentation state cannot mutate the Control Plane or Knowledge.

## Sources and safe rendering

Sources are keyboard-operable disclosures with titles, authority, status, and typed supporting fact identifiers when present. Each completed turn starts collapsed, and its source-list `hidden` state is explicitly enforced so CSS grid layout cannot reveal cards while `aria-expanded="false"`. The disclosure is low-chrome, aligned with the answer, separated by 16px from answer text and 9px from expanded cards. All AI and source content is inserted with DOM `textContent`. The Markdown subset supports headings, emphasis, lists, quotes, dividers, inline code, fenced code blocks, and copy actions. Raw HTML, remote resources, arbitrary commands, `innerHTML`, `eval`, and script injection are prohibited. The Webview CSP uses `default-src 'none'` and nonce-scoped scripts.

## State and lifecycle

The host owns a bounded, process-local session: at most 100 turns and 48,000 rendered characters. Only one Project QA request may be active per panel. Oldest completed turns are evicted before failed turns; an active turn is never evicted. The panel is reused while open and its session is intentionally non-durable.

Connection and readiness state comes only from existing Control Plane status, AI status, Knowledge component state, and capabilities. Setup, Repair, Reconnect, Health, and Diagnostics delegate to existing extension commands. Successful setup refreshes canonical state; failures preserve the previous connected/degraded state.

## Accessibility and responsive behavior

Long submitted questions use per-turn, bidirectional disclosure: `Show more` expands the complete stored question and `Show less` restores its bounded preview. Top-level conversation separators appear only between adjacent turns, never before the first or after the last. The composer region has no full-width divider or background band; its own surface and spacing provide separation.

Interactive elements use native buttons, explicit labels, `aria-expanded`, `aria-selected`, and `aria-pressed`. Focus uses VS Code focus colors. The design does not rely on color alone: state includes text. Source disclosure, tabs, composer, context chips, Retry, feedback, New Chat, and Inspector controls must be keyboard reachable. The permanent header Inspector control and Inspector × share one Webview-local visibility state, so a closed Inspector is always reopenable without resetting the conversation or selected turn.

## Component seams

The current production components are Header, Conversation, Turn, Safe Markdown, Code Block, Sources Disclosure, Answer Actions, Composer, Context Chips, and Inspector tabs. Future Agents/Activity and Market Intelligence modules may reuse these primitives only after their own typed data contracts and runtime APIs exist. They must not be prototyped with production fake data.

## Versioning and packaging

UX-A incremented the extension install identity to 0.1.10 with compact keyboard-accessible per-message actions, local Copy feedback, edit-to-draft without historical mutation, per-turn Sources disclosure, and near-bottom scroll anchoring with a Jump to latest control. The Inspector visual follow-up incremented it to 0.1.11, and the Sources/Inspector hidden-state hotfix to 0.1.12. The Sources-spacing and Inspector-reopen hotfix increments it to 0.1.13. Distribution contains one active versioned VSIX plus `SHA256SUMS`. A same-version replacement is prohibited because VS Code can retain stale installed code and make acceptance ambiguous. The Host-to-Webview session snapshot carries a monotonic revision; Webview discards older snapshots, so a terminal answer cannot be overwritten by a delayed pending snapshot for the same turn.

## Acceptance gates

Every visible control must be functional or explicitly disabled/unavailable. Required automated gates are TypeScript compile, extension tests, lint, package inspection, relevant Python regression tests, Ruff, Ruff format, pip check, and `git diff --check`. Required manual gates on Windows are install identity verification, Ready/model/Knowledge status, Workbench opening, two grounded Project QA prompts, Sources disclosure, Retry, feedback, context chips, New Chat, Inspector tabs, responsive layout, and no duplicate composer or false setup state.
