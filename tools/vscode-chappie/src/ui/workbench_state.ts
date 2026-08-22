import { ContextOptionsV1, WorkbenchContextAttachmentV1, WorkbenchTurnV1 } from "./workbench_protocol";

export const MAX_WORKBENCH_TURNS = 100;
export const MAX_WORKBENCH_RENDERED_CHARACTERS = 48_000;

export class WorkbenchSessionV1 {
  private turns: WorkbenchTurnV1[] = [];
  private revision = 0;
  public get active(): boolean { return this.turns.some((turn) => turn.status === "pending"); }
  public get currentRevision(): number { return this.revision; }
  public snapshot(): readonly WorkbenchTurnV1[] { return this.turns.map(copyTurn); }
  public begin(turnId: string, requestId: string, question: string, context: ContextOptionsV1, startedAtMs = Date.now(), attachments: readonly WorkbenchContextAttachmentV1[] = []): WorkbenchTurnV1 | undefined {
    if (this.active) return undefined;
    const turn: WorkbenchTurnV1 = { turnId, requestId, question, context: { ...context }, attachments: attachments.map(copyAttachment), status: "pending", startedAtMs };
    this.turns.push(turn); this.trim(); this.revision += 1; return copyTurn(turn);
  }
  public complete(turnId: string, response: WorkbenchTurnV1["response"], completedAtMs = Date.now()): WorkbenchTurnV1 | undefined { return this.settle(turnId, { response, status: "complete", completedAtMs }); }
  public fail(turnId: string, error: string, completedAtMs = Date.now()): WorkbenchTurnV1 | undefined { return this.settle(turnId, { error, status: "failed", completedAtMs }); }
  public find(turnId: string): WorkbenchTurnV1 | undefined { const turn = this.turns.find((item) => item.turnId === turnId); return turn ? copyTurn(turn) : undefined; }
  public feedback(turnId: string, value: "good" | "bad"): WorkbenchTurnV1 | undefined { const index = this.turns.findIndex((turn) => turn.turnId === turnId && turn.status === "complete"); if (index < 0) return undefined; this.turns[index] = { ...this.turns[index], feedback: this.turns[index].feedback === value ? null : value }; return { ...this.turns[index] }; }
  public clear(): void { this.turns = this.turns.filter((turn) => turn.status === "pending"); this.revision += 1; }
  private settle(turnId: string, update: Pick<WorkbenchTurnV1, "status" | "response" | "error" | "completedAtMs">): WorkbenchTurnV1 | undefined {
    const index = this.turns.findIndex((turn) => turn.turnId === turnId && turn.status === "pending"); if (index < 0) return undefined;
    if (update.status === "complete" && !(update.response?.answer ?? "").trim()) throw new Error("Completed Workbench turns require a non-empty Project QA answer.");
    this.turns[index] = { ...this.turns[index], ...update }; this.trim(); this.revision += 1; return { ...this.turns[index] };
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

function copyAttachment(attachment: WorkbenchContextAttachmentV1): WorkbenchContextAttachmentV1 { return { ...attachment }; }
function copyTurn(turn: WorkbenchTurnV1): WorkbenchTurnV1 { return { ...turn, context: { ...turn.context }, attachments: turn.attachments.map(copyAttachment) }; }
