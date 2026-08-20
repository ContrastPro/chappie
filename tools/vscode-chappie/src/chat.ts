import { ProjectQAResponseV1 } from "./contracts";

export interface ChatTurnV1 {
  question: string;
  response?: ProjectQAResponseV1;
  error?: string;
}

export const MAX_CHAT_TURNS = 20;

export function appendTurn(history: readonly ChatTurnV1[], turn: ChatTurnV1): ChatTurnV1[] {
  return [...history, turn].slice(-MAX_CHAT_TURNS);
}

export function escapeHtml(value: string): string {
  return value.replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character] ?? character);
}

export function renderTurns(history: readonly ChatTurnV1[]): string {
  return history.map((turn) => {
    const response = turn.response;
    const sources = response?.supporting_records.map((source) =>
      `<li>${escapeHtml(source.title)} (${escapeHtml(source.record_id)}) — ${escapeHtml(source.authority)} / ${escapeHtml(source.status)}</li>`
    ).join("") ?? "";
    const unknowns = response?.unknowns.map((unknown) => `<li>${escapeHtml(unknown)}</li>`).join("") ?? "";
    return `<section class="turn"><p class="label">You</p><p>${escapeHtml(turn.question)}</p><p class="label">Chappie</p><p>${escapeHtml(response?.answer ?? turn.error ?? "Unavailable")}</p>${sources ? `<details><summary>Sources (${response?.supporting_records.length ?? 0})</summary><ul>${sources}</ul></details>` : ""}${unknowns ? `<details><summary>Unknowns (${response?.unknowns.length ?? 0})</summary><ul>${unknowns}</ul></details>` : ""}</section>`;
  }).join("\n");
}
