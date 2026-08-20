import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { ChappieControlPlaneClient, ControlPlaneClientError, ControlPlaneDependencyError, ControlPlaneIncompatibleError } from "../client";
import { capabilitySummary, contextPreview, humanState, projectChatAvailability, projectQaFailureMessage } from "../presentation";
import { CONTROL_PLANE_DIGEST, CONTROL_PLANE_IDENTITY, TRANSPORT_DIGEST, TRANSPORT_IDENTITY } from "../contracts";
import { isChappieWorkspace, OwnedControlPlaneProcess, resolveChappieExecutable, shouldAutoStartControlPlane } from "../workspace";
import { appendTurn, ChatTurnV1, escapeHtml, MAX_CHAT_TURNS, renderTurns } from "../chat";
import { WorkspaceContextCollector } from "../workspace_context";
import { collectGitMetadata } from "../git_metadata";

function envelope(data: unknown, overrides: Record<string, unknown> = {}): Response {
  return new Response(JSON.stringify({
    transport_contract: TRANSPORT_IDENTITY,
    transport_contract_digest: TRANSPORT_DIGEST,
    control_plane_contract: CONTROL_PLANE_IDENTITY,
    control_plane_contract_digest: CONTROL_PLANE_DIGEST,
    request_id: "a".repeat(32), status: "OK", data, error: null, ...overrides,
  }), { status: 200, headers: { "content-type": "application/json" } });
}

test("health handshake validates exact identities", async () => {
  const client = new ChappieControlPlaneClient(8765, async () => envelope({ status: "OK" }));
  assert.deepEqual(await client.health(), { status: "OK" });
  await assert.rejects(() => new ChappieControlPlaneClient(8765, async () => envelope({ status: "OK" }, { transport_contract_digest: "wrong" })).health(), ControlPlaneIncompatibleError);
});

test("client is fixed to loopback and configured port", () => {
  assert.equal(new ChappieControlPlaneClient(9123).endpoint, "http://127.0.0.1:9123");
  assert.throws(() => new ChappieControlPlaneClient(0), ControlPlaneClientError);
});

test("status, AI, and capability payloads parse through validated envelopes", async () => {
  const values = [
    { identity: CONTROL_PLANE_IDENTITY, contract_digest: CONTROL_PLANE_DIGEST, state: "OK", status: { overall_state: "READY" }, error: null },
    { identity: CONTROL_PLANE_IDENTITY, contract_digest: CONTROL_PLANE_DIGEST, state: "OK", provider: "ollama", profile: "BALANCED", model: "qwen3:8b", provider_state: "READY", model_state: "READY", error: null },
    { identity: CONTROL_PLANE_IDENTITY, contract_digest: CONTROL_PLANE_DIGEST, state: "OK", capabilities: [{ capability_id: "system.status", status: "AVAILABLE" }], error: null },
  ];
  let index = 0;
  const client = new ChappieControlPlaneClient(8765, async () => envelope(values[index++]));
  assert.equal((await client.status()).status?.overall_state, "READY");
  assert.equal((await client.aiStatus()).model, "qwen3:8b");
  assert.equal((await client.capabilities()).capabilities[0].capability_id, "system.status");
});

test("malformed and timed-out responses fail safely", async () => {
  await assert.rejects(() => new ChappieControlPlaneClient(8765, async () => new Response("{}", { status: 200 })).health(), ControlPlaneClientError);
  await assert.rejects(() => new ChappieControlPlaneClient(8765, async () => { throw new DOMException("aborted", "AbortError"); }).health(), /timed out/);
  await assert.rejects(() => new ChappieControlPlaneClient(8765, async () => { throw new Error("connection refused"); }).health(), /unavailable/);
});

test("typed Project QA dependency errors map without exposing expected HTTP status", async () => {
  const unavailable = JSON.stringify({
    transport_contract: TRANSPORT_IDENTITY,
    transport_contract_digest: TRANSPORT_DIGEST,
    control_plane_contract: CONTROL_PLANE_IDENTITY,
    control_plane_contract_digest: CONTROL_PLANE_DIGEST,
    request_id: "a".repeat(32),
    status: "ERROR",
    data: null,
    error: { code: "AI_UNAVAILABLE", message: "Project QA is unavailable" },
  });
  await assert.rejects(
    () => new ChappieControlPlaneClient(8765, async () => new Response(unavailable, { status: 503, headers: { "content-type": "application/json" } })).askProject("Why unavailable?"),
    (error: unknown) => error instanceof ControlPlaneDependencyError && error.reasonCode === "AI_UNAVAILABLE",
  );
  assert.equal(projectQaFailureMessage("AI_UNAVAILABLE"), "Chappie AI is not configured for this workspace.");
  assert.equal(projectQaFailureMessage("KNOWLEDGE_UNAVAILABLE"), "Chappie Knowledge is not configured for this workspace.");
});

test("platform executable resolution and owned process stop are argv-safe", () => {
  assert.equal(resolveChappieExecutable("/repo", "darwin", () => true).command, "/repo/.venv/bin/chappie");
  assert.equal(resolveChappieExecutable("C:/repo", "win32", () => true).command, "C:/repo/.venv/Scripts/chappie.exe");
  assert.equal(resolveChappieExecutable("/repo", "linux", () => false).command, "chappie");
  let killed = 0;
  const owned = new OwnedControlPlaneProcess();
  assert.equal(owned.running, false);
  owned.start({ command: "chappie", args: ["serve"], cwd: "/repo" }, (_file, args, options) => { assert.deepEqual(args, ["serve"]); assert.equal(options.shell, false); return { kill: () => { killed += 1; return true; } }; });
  assert.equal(owned.stop(), true);
  assert.equal(owned.running, false);
  assert.equal(owned.stop(), false);
  assert.equal(killed, 1);
});

test("Chappie workspace detection requires all bounded repository markers", () => {
  const files = new Set(["/repo/pyproject.toml", "/repo/app", "/repo/app/cli.py"]);
  assert.equal(isChappieWorkspace("/repo", (candidate) => files.has(candidate), (candidate) => candidate === "/repo/app"), true);
  assert.equal(isChappieWorkspace("/python-project", () => true, () => false), false);
});

test("auto-start is limited to a recognized unavailable local Control Plane", () => {
  assert.equal(shouldAutoStartControlPlane(true, true, "DISCONNECTED"), true);
  assert.equal(shouldAutoStartControlPlane(true, true, "CONNECTED"), false);
  assert.equal(shouldAutoStartControlPlane(true, true, "INCOMPATIBLE"), false);
  assert.equal(shouldAutoStartControlPlane(false, true, "DISCONNECTED"), false);
  assert.equal(shouldAutoStartControlPlane(true, false, "DISCONNECTED"), false);
});

test("connected degraded Project QA remains connected and presents bounded remediation", () => {
  assert.equal(projectChatAvailability("CONNECTED", "DEGRADED", "NOT_CONFIGURED", "NOT_CONFIGURED"), "Project chat unavailable. AI and Knowledge setup required.");
  assert.equal(projectChatAvailability("CONNECTED", "DEGRADED", "NOT_CONFIGURED", "READY"), "Project chat unavailable. Chappie AI is not configured for this workspace.");
  assert.equal(projectChatAvailability("CONNECTED", "DEGRADED", "READY", "NOT_CONFIGURED"), "Project chat unavailable. Chappie Knowledge is not configured for this workspace.");
  assert.equal(projectChatAvailability("CONNECTED", "AVAILABLE", "READY", "READY"), null);
  assert.equal(capabilitySummary([{ status: "AVAILABLE" }, { status: "DEGRADED" }, { status: "UNAVAILABLE" }]), "1/3");
});

test("compact presentation translates states and provides explicit safe context previews", () => {
  assert.equal(humanState("SETUP_REQUIRED"), "Setup required");
  assert.equal(humanState("NOT_CONFIGURED"), "Not configured");
  assert.equal(humanState("READY"), "Ready");
  assert.equal(contextPreview({ active_file_relative_path: "app/control_plane.py", selected_text_range: { start_line: 119, end_line: 137 }, excerpt_range: null, git_branch: "main", git_dirty: true }), "app/control_plane.py · Lines 120–138\nGit · main · dirty");
  assert.equal(contextPreview(undefined), "No workspace context selected.");
});

test("project QA response, history, and renderer remain bounded and escaped", async () => {
  const qa = {
    identity: CONTROL_PLANE_IDENTITY, contract_digest: CONTROL_PLANE_DIGEST, state: "OK",
    question: "Who owns evidence?", answer: "RDE owns <evidence>.",
    supporting_knowledge_fact_ids: ["knowledge.record.statement"],
    supporting_records: [{ record_id: "record", title: "Evidence <authority>", authority: "VERSIONED_CONTRACT", status: "FROZEN" }],
    unknowns: ["<unknown>"], context_digest: "a".repeat(64), provider: "mock", model: "configured-model",
    authority: "AI_WITH_KNOWLEDGE_READ_ONLY", validation_status: "VALID", error: null,
  } as const;
  const client = new ChappieControlPlaneClient(8765, async () => envelope(qa));
  const response = await client.askProject("Who owns evidence?");
  assert.equal(response.supporting_records[0].record_id, "record");
  assert.equal(response.unknowns[0], "<unknown>");
  assert.match(renderTurns([{ question: "<question>", response }]), /&lt;question&gt;/);
  assert.doesNotMatch(renderTurns([{ question: "<question>", response }]), /<question>/);
  assert.equal(escapeHtml("<>&'\""), "&lt;&gt;&amp;&#39;&quot;");
  let history: ChatTurnV1[] = [];
  for (let index = 0; index < MAX_CHAT_TURNS + 1; index += 1) { history = appendTurn(history, { question: String(index) }); }
  assert.equal(history.length, MAX_CHAT_TURNS);
  await assert.rejects(() => client.askProject(" "));
  await assert.rejects(() => new ChappieControlPlaneClient(8765, async () => envelope({ answer: "missing sources" })).askProject("x"), ControlPlaneClientError);
});

test("workspace context is explicit, bounded, relative, and non-sensitive", () => {
  const collector = new WorkspaceContextCollector();
  const input = { workspaceRoot: "/repo", workspaceName: "repo", filePath: "/repo/app/ai/runtime.py", languageId: "python", text: "one\ntwo\nthree", selection: { start: 0, end: 3 }, cursorLine: 1, git: { branch: "main", commit: "a".repeat(40), dirty: true } };
  assert.equal(collector.collect(input, { includeFile: false, includeSelection: false, includeGit: false }), undefined);
  const context = collector.collect(input, { includeFile: true, includeSelection: true, includeGit: true });
  assert.equal(context?.active_file_relative_path, "app/ai/runtime.py");
  assert.equal(context?.selected_text, "one");
  assert.equal(context?.git_branch, "main");
  assert.doesNotMatch(JSON.stringify(context), /\/repo/);
  assert.equal(context?.context_digest, collector.collect(input, { includeFile: true, includeSelection: true, includeGit: true })?.context_digest);
  assert.throws(() => collector.collect({ ...input, filePath: "/outside/.env" }, { includeFile: true, includeSelection: false, includeGit: false }), /OUTSIDE/);
  assert.throws(() => collector.collect({ ...input, filePath: "/repo/.env" }, { includeFile: true, includeSelection: false, includeGit: false }), /SENSITIVE/);
});

test("git metadata is argv-bounded, opt-in data only, and omits unsafe output", () => {
  const calls: string[][] = [];
  const clean = collectGitMetadata("/repo", (args) => { calls.push([...args]); return args[0] === "status" ? "" : args[1] === "--abbrev-ref" ? "main\n" : `${"a".repeat(40)}\n`; });
  assert.deepEqual(clean, { branch: "main", commit: "a".repeat(40), dirty: false });
  assert.deepEqual(calls, [["rev-parse", "--abbrev-ref", "HEAD"], ["rev-parse", "HEAD"], ["status", "--porcelain"]]);
  assert.deepEqual(collectGitMetadata("C:/repo", (args) => args[0] === "status" ? " M secret.env\n" : args[1] === "--abbrev-ref" ? "HEAD\n" : `${"b".repeat(40)}\n`), { branch: null, commit: "b".repeat(40), dirty: true });
  assert.equal(collectGitMetadata("/not-repo", () => { throw new Error("not a repository"); }), null);
  const collector = new WorkspaceContextCollector();
  assert.equal(collector.collect({ workspaceRoot: "/repo", workspaceName: "repo" }, { includeFile: false, includeSelection: false, includeGit: false }), undefined);
  assert.doesNotMatch(JSON.stringify(clean), /secret|remote|url/i);
});

test("contributed Chappie view types match their registered providers", () => {
  const manifest = JSON.parse(readFileSync("package.json", "utf8")) as { contributes: { views: { chappie: Array<{ id: string; type?: string }> } } };
  const source = readFileSync("src/extension.ts", "utf8");
  for (const view of manifest.contributes.views.chappie) {
    if (view.type === "webview") {
      assert.match(source, new RegExp(`registerWebviewViewProvider\\("${view.id}"`));
    } else {
      assert.match(source, new RegExp(`registerTreeDataProvider\\("${view.id}"`));
    }
  }
});

test("activation remains bounded to the Chappie CLI marker", () => {
  const manifest = JSON.parse(readFileSync("package.json", "utf8")) as { activationEvents: string[] };
  assert.ok(manifest.activationEvents.includes("workspaceContains:**/app/cli.py"));
  assert.equal(manifest.activationEvents.includes("workspaceContains:pyproject.toml"), false);
});

test("setup remediation and detailed capabilities remain explicit commands", () => {
  const manifest = JSON.parse(readFileSync("package.json", "utf8")) as { contributes: { commands: Array<{ command: string }> } };
  assert.deepEqual(manifest.contributes.commands.map((item) => item.command).filter((item) => item.startsWith("chappie.show")), ["chappie.showDoctor", "chappie.showCapabilities", "chappie.showSetupInstructions"]);
  const source = readFileSync("src/extension.ts", "utf8");
  assert.match(source, /macOS local AI setup is not implemented/);
  assert.match(source, /Capabilities: \$\{capabilitySummary/);
  assert.match(source, /aria-pressed/);
  assert.match(source, /Sensitive file blocked\. Context was not sent/);
  assert.match(source, /--vscode-sideBar-background/);
  assert.match(source, /Project Chat Unavailable/);
});
