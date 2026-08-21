import { ContextOptionsV1, WorkbenchTurnV1 } from "./workbench_protocol";

export const MAX_WORKBENCH_TURNS = 100;
export const MAX_WORKBENCH_RENDERED_CHARACTERS = 48_000;

export class WorkbenchSessionV1 {
  private turns: WorkbenchTurnV1[] = [];
  public get active(): boolean { return this.turns.some((turn) => turn.status === "pending"); }
  public snapshot(): readonly WorkbenchTurnV1[] { return this.turns.map((turn) => ({ ...turn })); }
  public begin(turnId: string, requestId: string, question: string, context: ContextOptionsV1): WorkbenchTurnV1 | undefined {
    if (this.active) return undefined;
    const turn: WorkbenchTurnV1 = { turnId, requestId, question, context, status: "pending" };
    this.turns.push(turn); this.trim(); return { ...turn };
  }
  public complete(turnId: string, response: WorkbenchTurnV1["response"]): WorkbenchTurnV1 | undefined { return this.settle(turnId, { response, status: "complete" }); }
  public fail(turnId: string, error: string): WorkbenchTurnV1 | undefined { return this.settle(turnId, { error, status: "failed" }); }
  public clear(): void { this.turns = this.turns.filter((turn) => turn.status === "pending"); }
  private settle(turnId: string, update: Pick<WorkbenchTurnV1, "status" | "response" | "error">): WorkbenchTurnV1 | undefined {
    const index = this.turns.findIndex((turn) => turn.turnId === turnId && turn.status === "pending"); if (index < 0) return undefined;
    this.turns[index] = { ...this.turns[index], ...update }; this.trim(); return { ...this.turns[index] };
  }
  private trim(): void {
    while (this.turns.length > MAX_WORKBENCH_TURNS || this.characters() > MAX_WORKBENCH_RENDERED_CHARACTERS) {
      const completed = this.turns.findIndex((turn) => turn.status === "complete");
      const index = completed >= 0 ? completed : this.turns.findIndex((turn) => turn.status === "failed");
      if (index < 0) return;
      this.turns.splice(index, 1);
    }
  }
  private characters(): number { return this.turns.reduce((size, turn) => size + turn.question.length + (turn.response?.answer?.length ?? 0) + (turn.error?.length ?? 0), 0); }
}
