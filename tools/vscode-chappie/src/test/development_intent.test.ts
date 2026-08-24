import assert from "node:assert/strict";
import test from "node:test";
import { DEVELOPMENT_INTENT_OPTIONS, DevelopmentIntentSelectionV1 } from "../ui/development_intent";
import { WorkbenchSessionV1 } from "../ui/workbench_state";

const noContext = { includeFile: false, includeSelection: false, includeGit: false };

test("Development intent selector defaults and resets to Question", () => {
  const selection = new DevelopmentIntentSelectionV1();
  assert.equal(selection.current, "QUESTION");
  selection.select("IMPLEMENTATION");
  assert.equal(selection.current, "IMPLEMENTATION");
  selection.reset();
  assert.equal(selection.current, "QUESTION");
});

test("Development intent selector exposes exactly four explicit mappings", () => {
  assert.deepEqual(DEVELOPMENT_INTENT_OPTIONS.map(({ label, intent }) => ({ label, intent })), [
    { label: "Question", intent: "QUESTION" },
    { label: "Analysis", intent: "ANALYSIS" },
    { label: "Plan", intent: "PLAN" },
    { label: "Implementation", intent: "IMPLEMENTATION" },
  ]);
});

test("turns retain explicit intent independently from later selector changes", () => {
  const session = new WorkbenchSessionV1();
  const question = session.begin("question", "request_question", "Implement this change.", noContext, 1, [], "DEVELOPMENT", "QUESTION");
  assert.equal(question?.requestedIntent, "QUESTION");
  session.fail(question!.turnId, "fixture");
  const plan = session.begin("plan", "request_plan", "Plan this change.", noContext, 2, [], "DEVELOPMENT", "PLAN");
  assert.equal(plan?.requestedIntent, "PLAN");
  assert.equal(session.find(question!.turnId)?.requestedIntent, "QUESTION");
});

test("CHAT turns never retain Development intent", () => {
  const session = new WorkbenchSessionV1();
  const turn = session.begin("chat", "request_chat", "Implement this change.", noContext, 1, [], "CHAT", "IMPLEMENTATION");
  assert.equal(turn?.requestedIntent, undefined);
});
