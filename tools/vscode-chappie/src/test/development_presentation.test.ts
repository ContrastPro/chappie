import assert from "node:assert/strict";
import test from "node:test";
import { DevelopmentAnalysisV1, DevelopmentOutcomeV1 } from "../api/contracts";
import { buildDevelopmentPresentation, developmentPresentationText } from "../ui/development_presentation";

const outcome = (status: DevelopmentOutcomeV1["status"], explanation = "The bounded request completed."): DevelopmentOutcomeV1 => ({
  identity: "chappie_development_outcome/1.1.0", status, blocker_code: status === "ANSWER" ? null : "TECHNICAL_CODE",
  explanation, required_context: null, suggested_next_action: "Choose the next bounded action.", safe_to_retry: false,
  request_id: "request-secret", failed_stage: status === "ANSWER" ? null : "REVIEW_PARSE", reason_code: status === "ANSWER" ? null : "TECHNICAL_CODE",
  no_changes_made: true, why_it_blocks: status === "ANSWER" ? "" : "The bounded validation gate did not pass.",
});

const analysis = (intent: "QUESTION" | "ANALYSIS" | "PLAN" | "IMPLEMENTATION", status: DevelopmentOutcomeV1["status"] = "ANSWER"): DevelopmentAnalysisV1 => ({
  identity: "chappie_development_analysis/2.0.0", state: "OK", request: "bounded request", evidence: null,
  facts: ["The Development service is read-only."], inferences: ["The adapter boundary limits authority."], unknowns: ["Runtime load is not measured."],
  architecture_owners: ["app.development_analysis"], affected_contracts: ["DevelopmentAnalysisV1"], likely_files: ["app/development_analysis/service.py"],
  risks: ["Contract drift may break consumers."], required_tests: ["Validate the bounded route."], non_goals: [], acceptance_criteria: ["Human approval remains required."],
  confidence: 0.9, validation_status: "VALID", readiness: intent === "IMPLEMENTATION" ? "READY_FOR_TASK" : null, reason_code: null,
  lifecycle: null, context_map: null, current_evidence_handles: [], proposed_scope: intent === "IMPLEMENTATION" ? {
    proposed_owner: "app.development_analysis", proposed_modified_files: ["app/development_analysis/service.py"], proposed_new_files: [],
    proposed_contract_changes: ["DevelopmentAnalysisV1"], proposed_components: ["DevelopmentAnalysisServiceV1"], proposed_behavior: ["Return a bounded advisory result."], rationale: ["Preserve authority boundaries."],
  } : null,
  task_intent: { identity: "chappie_development_task_intent/1.0.0", intent, source: "EXPLICIT", rule_id: "test", requesting_evidence_digest: "e".repeat(64), requesting_context_digest: "c".repeat(64) },
  answer: intent === "QUESTION" ? "Development is a read-only advisory pipeline." : null,
  plan_items: intent === "PLAN" ? ["Inspect bounded evidence.", "Review the proposed scope.", "Request implementation explicitly."] : [], outcome: outcome(status),
});

test("QUESTION presentation starts with Summary and excludes technical DTO metadata", () => {
  const value = buildDevelopmentPresentation(analysis("QUESTION"));
  const text = developmentPresentationText(value);
  assert.equal(value.summary, "Development is a read-only advisory pipeline.");
  assert.equal(value.sections[0]?.heading, "Technical facts");
  assert.match(text, /^Summary\n/);
  for (const forbidden of ["request-secret", "TECHNICAL_CODE", "evidence_digest", "context_digest", "knowledge_manifest_digest", "record_ids", "lifecycle"]) assert.doesNotMatch(text, new RegExp(forbidden, "i"));
  assert.match(text, /Status\nCompleted$/);
});

test("ANALYSIS renders findings, risks, limitations, next action, and completed status", () => {
  const value = buildDevelopmentPresentation(analysis("ANALYSIS"));
  assert.deepEqual(value.sections.map((item) => item.heading), ["Technical findings", "Risks", "Unknowns / limitations"]);
  assert.match(developmentPresentationText(value), /Next action\nChoose the next bounded action\./);
});

test("PLAN renders an ordered advisory plan without implementation authorization", () => {
  const source = analysis("PLAN");
  source.outcome!.suggested_next_action = "This plan is advisory. Select a bounded item and explicitly request IMPLEMENTATION.";
  const value = buildDevelopmentPresentation(source);
  const plan = value.sections.find((item) => item.heading === "Proposed plan");
  assert.equal(plan?.ordered, true);
  assert.match(developmentPresentationText(value), /1\. Inspect bounded evidence\.[\s\S]*3\. Request implementation explicitly\./);
  assert.match(developmentPresentationText(value), /This plan is advisory/);
  assert.doesNotMatch(developmentPresentationText(value), /authorized|ready for task/i);
});

test("IMPLEMENTATION renders scope, verification, readiness, and only valid Handoff eligibility", () => {
  const value = buildDevelopmentPresentation(analysis("IMPLEMENTATION", "NEEDS_HUMAN_REVIEW"));
  assert.deepEqual(value.sections.map((item) => item.heading), ["Proposed scope", "Technical findings", "Risks", "Verification / tests", "Readiness or blocker", "Unknowns / limitations"]);
  assert.equal(value.allowHandoff, true);
  assert.equal(value.status, "Needs human review");
});

test("PARTIAL preserves validated value, labels Proposal, and denies Handoff", () => {
  const value = analysis("IMPLEMENTATION", "INTERNAL_FAILURE");
  value.state = "ANALYSIS_INVALID"; value.validation_status = "PARTIAL"; value.readiness = null;
  const presentation = buildDevelopmentPresentation(value);
  assert.equal(presentation.status, "Partial — implementation not authorized");
  assert.equal(presentation.allowHandoff, false);
  assert.ok(presentation.sections.some((item) => item.heading === "PROPOSED — IMPLEMENTATION NOT AUTHORIZED"));
  assert.match(developmentPresentationText(presentation), /What succeeded/);
});

for (const [status, label] of [["TASK_TOO_BROAD", "Task too broad"], ["TEMPORARY_FAILURE", "Temporary failure"], ["MODEL_UNAVAILABLE", "Model unavailable"], ["NEEDS_CONTEXT", "Needs context"]] as const) {
  test(`${status} is a human-readable product outcome`, () => {
    const value = analysis("IMPLEMENTATION", status); value.state = "ANALYSIS_INVALID"; value.readiness = null; value.proposed_scope = null;
    const presentation = buildDevelopmentPresentation(value);
    const text = developmentPresentationText(presentation);
    assert.equal(presentation.status, label);
    assert.match(text, /What blocked completion/);
    assert.doesNotMatch(text, /TECHNICAL_CODE|request-secret|REVIEW_PARSE/);
    assert.equal(presentation.allowHandoff, false);
  });
}

test("empty user-facing sections are omitted", () => {
  const value = analysis("QUESTION"); value.facts = []; value.unknowns = [];
  assert.deepEqual(buildDevelopmentPresentation(value).sections, []);
});
