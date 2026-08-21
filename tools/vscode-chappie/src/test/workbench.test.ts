import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { ProjectQAResponseV1 } from "../api/contracts";
import { workbenchMarkdownRendererScript } from "../ui/markdown";
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
  assert.match(script, /context\[key\]/);
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

test("Source disclosures remain DOM-local while unrelated turns are patched", () => {
  const script = workbenchRendererScript();
  assert.match(script, /const toggle=button\('Sources/);
  assert.match(script, /list\.hidden=!expanded/);
  assert.match(script, /section\.replaceWith\(turnNode\(turn\)\)/);
  assert.match(script, /const patchTurn=turn/);
});

test("Workbench exposes real retry feedback and bounded Inspector metadata", () => {
  const script = workbenchRendererScript();
  assert.match(script, /type:'retry'/);
  assert.match(script, /type:'feedback'/);
  assert.match(script, /validation_status/);
  assert.match(script, /supporting_records/);
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
});
