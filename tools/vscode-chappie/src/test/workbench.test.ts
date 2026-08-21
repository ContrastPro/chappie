import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { ProjectQAResponseV1 } from "../api/contracts";
import { workbenchMarkdownRendererScript } from "../ui/markdown";
import { WORKBENCH_COMPOSER_MAX_HEIGHT_PX, WORKBENCH_COMPOSER_MIN_HEIGHT_PX, workbenchComposerSize } from "../ui/composer";
import { parseWebviewMessage, WORKBENCH_PROTOCOL_VERSION } from "../ui/workbench_protocol";
import { workbenchRendererScript } from "../ui/workbench_render";
import { MAX_WORKBENCH_TURNS, WorkbenchSessionV1 } from "../ui/workbench_state";

const noContext = { includeFile: false, includeSelection: false, includeGit: false };

const response = (answer: string): ProjectQAResponseV1 => ({
  identity: "chappie_control_plane/1.2.0",
  contract_digest: "a".repeat(64),
  state: "OK",
  question: "question",
  answer,
  supporting_knowledge_fact_ids: [],
  supporting_records: [],
  unknowns: [],
  context_digest: "b".repeat(64),
  provider: "mock",
  model: "mock",
  authority: "AI_WITH_KNOWLEDGE_READ_ONLY",
  validation_status: "VALID",
  error: null,
});

test("Workbench protocol accepts only bounded typed webview messages", () => {
  assert.deepEqual(parseWebviewMessage({ type: "ready", protocol: WORKBENCH_PROTOCOL_VERSION }), { type: "ready", protocol: WORKBENCH_PROTOCOL_VERSION });
  assert.deepEqual(parseWebviewMessage({ type: "ask", requestId: "request_1", question: "Explain architecture", context: { includeFile: true, includeSelection: false, includeGit: true } }), { type: "ask", requestId: "request_1", question: "Explain architecture", context: { includeFile: true, includeSelection: false, includeGit: true } });
  assert.equal(parseWebviewMessage({ type: "ask", requestId: "request_1", question: " ", context: {} }), undefined);
  assert.equal(parseWebviewMessage({ type: "ask", requestId: "request_1", question: "x".repeat(1001), context: {} }), undefined);
  assert.equal(parseWebviewMessage({ type: "ask", requestId: "request_1", question: "x".repeat(12_001), context: noContext }), undefined);
  assert.equal(parseWebviewMessage({ type: "runAction", action: "unsafe" }), undefined);
  assert.deepEqual(parseWebviewMessage({ type: "retry", turnId: "turn_1" }), { type: "retry", turnId: "turn_1" });
  assert.deepEqual(parseWebviewMessage({ type: "feedback", turnId: "turn_1", value: "good" }), { type: "feedback", turnId: "turn_1", value: "good" });
  assert.equal(parseWebviewMessage({ type: "feedback", turnId: "turn_1", value: "maybe" }), undefined);
  assert.equal(parseWebviewMessage({ type: "ready", protocol: "wrong" }), undefined);
});

test("Workbench session permits one active request and preserves it when trimming", () => {
  const session = new WorkbenchSessionV1();
  const first = session.begin("turn_1", "request_1", "First", noContext);
  assert.ok(first);
  assert.equal(session.begin("turn_2", "request_2", "Second", noContext), undefined);
  session.fail("turn_1", "Unavailable");
  for (let index = 2; index <= MAX_WORKBENCH_TURNS; index += 1) {
    const turn = session.begin(`turn_${index}`, `request_${index}`, String(index), noContext);
    assert.ok(turn);
    session.complete(turn.turnId, response(String(index)));
  }
  const active = session.begin("turn_active", "request_active", "Active", noContext);
  assert.ok(active);
  const snapshot = session.snapshot();
  assert.ok(snapshot.length <= MAX_WORKBENCH_TURNS);
  assert.equal(snapshot.find((turn) => turn.status === "pending")?.turnId, "turn_active");
  assert.equal(snapshot.some((turn) => turn.turnId === "turn_1"), true);
});

test("Workbench clear keeps an in-flight request and returned snapshots are detached", () => {
  const session = new WorkbenchSessionV1();
  const pending = session.begin("turn_1", "request_1", "Question", noContext);
  assert.ok(pending);
  const snapshot = session.snapshot();
  snapshot[0].question = "Changed outside";
  assert.equal(session.snapshot()[0].question, "Question");
  session.clear();
  assert.equal(session.snapshot().length, 1);
  assert.equal(session.snapshot()[0].turnId, "turn_1");
});

test("Workbench terminal snapshots make completed and failed turns internally consistent", () => {
  const session = new WorkbenchSessionV1();
  const successful = session.begin("turn_success", "request_success", "Question", noContext, 100);
  assert.ok(successful);
  const realShape = response("A grounded final answer.");
  realShape.supporting_records = [{ record_id: "architecture", title: "Chappie Architecture", authority: "DOCUMENTATION", status: "ACTIVE" }];
  session.complete(successful.turnId, realShape, 200);
  const completed = session.find(successful.turnId);
  assert.equal(completed?.requestId, "request_success");
  assert.equal(completed?.status, "complete");
  assert.equal(completed?.response?.answer, "A grounded final answer.");
  assert.equal(completed?.response?.supporting_records[0]?.record_id, "architecture");
  assert.equal(completed?.completedAtMs, 200);

  const rejected = session.begin("turn_failure", "request_failure", "Question", noContext, 300);
  assert.ok(rejected);
  session.fail(rejected.turnId, "Knowledge is stale. Repair Chappie to use Project QA.", 400);
  const failed = session.find(rejected.turnId);
  assert.equal(failed?.requestId, "request_failure");
  assert.equal(failed?.status, "failed");
  assert.equal(failed?.response, undefined);
  assert.match(failed?.error ?? "", /Knowledge is stale/);
});

test("Workbench rejects a completed lifecycle snapshot without a final answer", () => {
  const session = new WorkbenchSessionV1();
  const pending = session.begin("turn_1", "request_1", "Question", noContext, 100);
  assert.ok(pending);
  assert.throws(() => session.complete(pending.turnId, response("   "), 200), /non-empty Project QA answer/);
  assert.equal(session.find(pending.turnId)?.status, "pending");
});

test("production-shaped replaceSession projects a terminal turn over the same pending turnId", () => {
  const session = new WorkbenchSessionV1();
  const pending = session.begin("turn_real", "request_real", "Understand architecture", noContext, 100);
  assert.ok(pending);
  const pendingSnapshot = session.snapshot();
  const pendingRevision = session.currentRevision;
  const terminalResponse = response("Chappie Architecture defines the system boundaries.");
  terminalResponse.supporting_records = [{ record_id: "architecture", title: "Chappie Architecture", authority: "DOCUMENTATION", status: "ACTIVE" }];
  session.complete(pending.turnId, terminalResponse, 200);
  const terminalSnapshot = session.snapshot();
  const terminalRevision = session.currentRevision;

  assert.equal(pendingSnapshot[0].turnId, terminalSnapshot[0].turnId);
  assert.equal(pendingSnapshot[0].requestId, terminalSnapshot[0].requestId);
  assert.equal(pendingSnapshot[0].status, "pending");
  assert.equal(terminalSnapshot[0].status, "complete");
  assert.match(terminalSnapshot[0].response?.answer ?? "", /system boundaries/);
  assert.equal(terminalSnapshot[0].response?.supporting_records[0]?.title, "Chappie Architecture");
  assert.equal(terminalSnapshot[0].response?.authority, "AI_WITH_KNOWLEDGE_READ_ONLY");
  assert.equal(terminalSnapshot[0].response?.validation_status, "VALID");
  assert.ok(terminalRevision > pendingRevision);
});

test("inline composer sizing is compact, grows, caps, and shrinks deterministically", () => {
  assert.deepEqual(workbenchComposerSize(0), { heightPx: WORKBENCH_COMPOSER_MIN_HEIGHT_PX, overflowY: "hidden" });
  assert.deepEqual(workbenchComposerSize(84), { heightPx: 84, overflowY: "hidden" });
  assert.deepEqual(workbenchComposerSize(WORKBENCH_COMPOSER_MAX_HEIGHT_PX), { heightPx: WORKBENCH_COMPOSER_MAX_HEIGHT_PX, overflowY: "hidden" });
  assert.deepEqual(workbenchComposerSize(WORKBENCH_COMPOSER_MAX_HEIGHT_PX + 1), { heightPx: WORKBENCH_COMPOSER_MAX_HEIGHT_PX, overflowY: "auto" });
  assert.deepEqual(workbenchComposerSize(60), { heightPx: 60, overflowY: "hidden" });
});

test("Workbench eviction prefers completed turns over failed turns", () => {
  const session = new WorkbenchSessionV1();
  const failed = session.begin("failed", "request_failed", "Failed", noContext);
  assert.ok(failed);
  session.fail(failed.turnId, "Unavailable");
  for (let index = 1; index <= MAX_WORKBENCH_TURNS; index += 1) {
    const turn = session.begin(`complete_${index}`, `request_complete_${index}`, String(index), noContext);
    assert.ok(turn);
    session.complete(turn.turnId, response(String(index)));
  }
  const snapshot = session.snapshot();
  assert.equal(snapshot.some((turn) => turn.turnId === "failed"), true);
  assert.equal(snapshot.some((turn) => turn.turnId === "complete_1"), false);
});

test("Workbench renderer uses incremental DOM-safe turn rendering", () => {
  const script = workbenchRendererScript();
  assert.match(script, /textContent/);
  assert.match(script, /\.append\(/);
  assert.match(script, /replaceChildren/);
  assert.doesNotMatch(script, /innerHTML/);
  assert.match(script, /previewContext/);
  assert.match(script, /clearConversation/);
  assert.match(script, /aria-expanded/);
  assert.match(script, /source-list/);
  assert.match(script, /navigator\.clipboard/);
  assert.match(script, /scrollHeight-canvas\.scrollTop/);
  assert.match(script, /attachments\.some/);
  assert.match(script, /expandedQuestions/);
  assert.match(script, /Show less/);
  assert.match(script, /aria-expanded/);
  assert.match(script, /\.turn\+\.turn/);
  assert.match(script, /composer-region\{border-top:0/);
});

test("Workbench Markdown subset builds inert DOM nodes for untrusted text", () => {
  const script = workbenchMarkdownRendererScript();
  assert.match(script, /document\.createElement/);
  assert.match(script, /textContent/);
  assert.match(script, /code-block/);
  assert.match(script, /blockquote/);
  assert.match(script, /startsWith\(fence\)/);
  assert.doesNotMatch(script, /innerHTML|insertAdjacentHTML|eval\(/);
  assert.doesNotMatch(script, /https?:\/\//);
});

test("Source disclosures remain DOM-local while canonical session snapshots are rendered", () => {
  const script = workbenchRendererScript();
  assert.match(script, /const toggleSources=/);
  assert.match(script, /list\.hidden=open/);
  assert.match(script, /turns\.replaceChildren\(\.\.\.session\.map\(turnNode\)\)/);
  assert.match(script, /const patchFeedback=turn/);
  assert.match(script, /const renderSession=\(next,revision\)/);
});

test("Workbench exposes real retry feedback and bounded Inspector metadata", () => {
  const script = workbenchRendererScript();
  assert.match(script, /type:'retry'/);
  assert.match(script, /type:'feedback'/);
  assert.match(script, /turn\.response\?\.authority/);
  assert.match(script, /turn\.response\?\.sources/);
  assert.match(script, /No supported Chappie–Codex runtime integration is configured/);
  assert.doesNotMatch(script, /chain.of.thought|fake agent|fake market/i);
});

test("Workbench feedback is local typed state and retry source remains immutable", () => {
  const session = new WorkbenchSessionV1();
  const turn = session.begin("turn_1", "request_1", "Question", noContext, 100);
  assert.ok(turn);
  session.complete(turn.turnId, response("Answer"), 125);
  assert.equal(session.feedback(turn.turnId, "good")?.feedback, "good");
  assert.equal(session.feedback(turn.turnId, "good")?.feedback, null);
  assert.equal(session.find(turn.turnId)?.question, "Question");
  assert.equal(session.find(turn.turnId)?.completedAtMs, 125);
});

test("Workbench controller keeps one reusable editor panel", () => {
  const source = readFileSync("src/ui/workbench.ts", "utf8");
  assert.match(source, /if \(this\.panel\) \{ this\.panel\.reveal/);
  assert.match(source, /this\.panel\.onDidDispose/);
  assert.match(source, /createWebviewPanel/);
  assert.match(source, /postSession/);
  assert.match(source, /replaceSession/);
  assert.match(source, /this\.connection\.availability/);
  assert.doesNotMatch(source, /operationChanged/);
  assert.match(source, /clearInterval\(telemetry\)/);
});

test("Workbench renderer projects status and lifecycle from host-owned snapshots", () => {
  const script = workbenchRendererScript();
  assert.match(script, /active=session\.some\(turn=>turn\.status==='pending'\)/);
  assert.match(script, /connection\.system==='READY'/);
  assert.match(script, /Needs attention/);
  assert.match(script, /enforceAvailability/);
  assert.match(script, /connection\?\.availability/);
  assert.match(script, /sessionRevision=-1/);
  assert.match(script, /revision<sessionRevision/);
  assert.match(script, /renderSession\(data\.session,data\.sessionRevision\)/);
  assert.match(script, /turn\.status==='pending'/);
  assert.match(script, /resizeComposer/);
  assert.match(script, /question\.scrollHeight/);
  assert.match(script, /overflowY=scrollHeight>220\?'auto':'hidden'/);
  assert.doesNotMatch(script, /turnCompleted\|\|data\.type==='turnFailed'/);
});
