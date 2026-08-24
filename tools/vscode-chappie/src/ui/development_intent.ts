import { DevelopmentTaskIntentKindV1 } from "../api/contracts";

export interface DevelopmentIntentOptionV1 { label: string; intent: DevelopmentTaskIntentKindV1; description: string; }

export const DEVELOPMENT_INTENT_OPTIONS: readonly DevelopmentIntentOptionV1[] = Object.freeze([
  Object.freeze({ label: "Question", intent: "QUESTION", description: "Answer a bounded development question" }),
  Object.freeze({ label: "Analysis", intent: "ANALYSIS", description: "Analyze findings, risks, and unknowns" }),
  Object.freeze({ label: "Plan", intent: "PLAN", description: "Prepare an advisory bounded plan" }),
  Object.freeze({ label: "Implementation", intent: "IMPLEMENTATION", description: "Run the strict implementation-readiness pipeline" }),
]);

export class DevelopmentIntentSelectionV1 {
  private selected: DevelopmentTaskIntentKindV1 = "QUESTION";
  public get current(): DevelopmentTaskIntentKindV1 { return this.selected; }
  public select(intent: DevelopmentTaskIntentKindV1): void { this.selected = intent; }
  public reset(): void { this.selected = "QUESTION"; }
}
