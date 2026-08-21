import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { ProjectQAResponseV1 } from "../api/contracts";
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
});

test("Workbench controller keeps one reusable editor panel", () => {
  const source = readFileSync("src/ui/workbench.ts", "utf8");
  assert.match(source, /if \(this\.panel\) \{ this\.panel\.reveal/);
  assert.match(source, /this\.panel\.onDidDispose/);
  assert.match(source, /createWebviewPanel/);
});
