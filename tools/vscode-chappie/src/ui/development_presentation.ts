import { DevelopmentAnalysisV1, DevelopmentOutcomeV1, DevelopmentProposedScopeV1 } from "../api/contracts";

export interface DevelopmentPresentationSectionV1 { heading: string; items: string[]; ordered: boolean; }
export interface DevelopmentPresentationV1 { summary: string; sections: DevelopmentPresentationSectionV1[]; nextAction: string | null; status: string; allowHandoff: boolean; }

const STATUS_LABELS: Readonly<Record<DevelopmentOutcomeV1["status"], string>> = Object.freeze({
  ANSWER: "Completed", NEEDS_CONTEXT: "Needs context", TASK_TOO_BROAD: "Task too broad",
  NEEDS_HUMAN_REVIEW: "Needs human review", UNSUPPORTED_CAPABILITY: "Unsupported capability",
  MODEL_UNAVAILABLE: "Model unavailable", TEMPORARY_FAILURE: "Temporary failure", INTERNAL_FAILURE: "Internal failure",
});

const section = (heading: string, items: readonly (string | null | undefined)[], ordered = false): DevelopmentPresentationSectionV1 | null => {
  const values = items.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
  return values.length ? { heading, items: values, ordered } : null;
};

const scopeItems = (scope: DevelopmentProposedScopeV1 | null): string[] => scope ? [
  scope.proposed_owner ? `Owner: ${scope.proposed_owner}` : null,
  ...scope.proposed_modified_files.map((item) => `Modify: ${item}`),
  ...scope.proposed_new_files.map((item) => `Create: ${item}`),
  ...scope.proposed_contract_changes.map((item) => `Contract: ${item}`),
  ...scope.proposed_components.map((item) => `Component: ${item}`),
  ...scope.proposed_behavior.map((item) => `Behavior: ${item}`),
  ...scope.rationale.map((item) => `Rationale: ${item}`),
].filter((item): item is string => item !== null) : [];

const findings = (analysis: DevelopmentAnalysisV1): string[] => [
  ...analysis.facts, ...analysis.inferences,
  ...analysis.architecture_owners.map((item) => `Owner: ${item}`),
  ...analysis.affected_contracts.map((item) => `Contract: ${item}`),
  ...analysis.likely_files.map((item) => `File: ${item}`),
];

const fallbackSummary = (analysis: DevelopmentAnalysisV1): string => analysis.outcome?.explanation || analysis.facts[0] || "Chappie completed the bounded Development response.";

const failurePresentation = (analysis: DevelopmentAnalysisV1): DevelopmentPresentationV1 => {
  const outcome = analysis.outcome;
  const partial = analysis.validation_status === "PARTIAL";
  const succeeded = [...analysis.facts, ...analysis.inferences, ...(analysis.proposed_scope ? ["A bounded Proposal passed validation before the later stage was blocked."] : [])];
  const sections = [
    section("What succeeded", succeeded),
    analysis.proposed_scope ? section("PROPOSED — IMPLEMENTATION NOT AUTHORIZED", scopeItems(analysis.proposed_scope)) : null,
    section("What blocked completion", [outcome?.explanation || "The later implementation stage did not pass bounded validation.", outcome?.why_it_blocks]),
    section("What remains safely known", analysis.unknowns.length ? [...analysis.facts, ...analysis.unknowns.map((item) => `Limitation: ${item}`)] : analysis.facts),
  ].filter((item): item is DevelopmentPresentationSectionV1 => item !== null);
  return { summary: fallbackSummary(analysis), sections, nextAction: outcome?.suggested_next_action || null, status: partial ? "Partial — implementation not authorized" : STATUS_LABELS[outcome!.status], allowHandoff: false };
};

export function buildDevelopmentPresentation(analysis: DevelopmentAnalysisV1): DevelopmentPresentationV1 {
  const outcome = analysis.outcome;
  const intent = analysis.task_intent?.intent ?? "IMPLEMENTATION";
  if (analysis.validation_status === "PARTIAL" || (outcome && !["ANSWER", "NEEDS_HUMAN_REVIEW"].includes(outcome.status))) return failurePresentation(analysis);
  const sections: (DevelopmentPresentationSectionV1 | null)[] = [];
  if (intent === "QUESTION") sections.push(section("Technical facts", analysis.facts));
  else if (intent === "ANALYSIS") sections.push(section("Technical findings", findings(analysis)), section("Risks", analysis.risks));
  else if (intent === "PLAN") sections.push(section("Proposed plan", analysis.plan_items, true), section("Dependencies / risks", analysis.risks));
  else sections.push(
    section("Proposed scope", scopeItems(analysis.proposed_scope)), section("Technical findings", findings(analysis)),
    section("Risks", analysis.risks), section("Verification / tests", [...analysis.required_tests, ...analysis.acceptance_criteria]),
    section("Readiness or blocker", [analysis.readiness ? analysis.readiness.replaceAll("_", " ").toLowerCase() : null, outcome?.explanation]),
  );
  sections.push(section("Unknowns / limitations", analysis.unknowns));
  return {
    summary: analysis.answer || fallbackSummary(analysis),
    sections: sections.filter((item): item is DevelopmentPresentationSectionV1 => item !== null),
    nextAction: outcome?.suggested_next_action || null,
    status: outcome ? STATUS_LABELS[outcome.status] : "Completed",
    allowHandoff: intent === "IMPLEMENTATION" && analysis.state === "OK" && analysis.validation_status === "VALID" && analysis.readiness === "READY_FOR_TASK" && outcome?.status === "NEEDS_HUMAN_REVIEW",
  };
}

export function developmentPresentationText(presentation: DevelopmentPresentationV1): string {
  const blocks = [`Summary\n${presentation.summary}`];
  for (const value of presentation.sections) blocks.push(`${value.heading}\n${value.items.map((item, index) => value.ordered ? `${index + 1}. ${item}` : `- ${item}`).join("\n")}`);
  if (presentation.nextAction) blocks.push(`Next action\n${presentation.nextAction}`);
  blocks.push(`Status\n${presentation.status}`);
  return blocks.join("\n\n");
}
