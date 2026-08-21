import { createHash } from "node:crypto";
import * as path from "node:path";

export const WORKSPACE_CONTEXT_IDENTITY = "workspace_context/1.0.0";
export const MAX_CONTEXT_CHARACTERS = 12_000;

export interface ContextRangeV1 { start_line: number; end_line: number; }
export interface WorkspaceContextV1 {
  schema: typeof WORKSPACE_CONTEXT_IDENTITY; workspace_name: string; workspace_id: string;
  active_file_relative_path: string | null; language_id: string | null; selected_text: string | null;
  selected_text_range: ContextRangeV1 | null; active_file_excerpt: string | null; excerpt_range: ContextRangeV1 | null;
  git_branch: string | null; git_commit: string | null; git_dirty: boolean | null;
  selection_truncated: boolean; excerpt_truncated: boolean; context_digest: string;
}

export interface EditorContextInputV1 { workspaceRoot: string; workspaceName: string; filePath?: string; languageId?: string; text?: string; selection?: { start: number; end: number }; cursorLine?: number; git?: { branch: string | null; commit: string; dirty: boolean }; }

export class WorkspaceContextCollector {
  public collect(input: EditorContextInputV1, options: { includeFile: boolean; includeSelection: boolean; includeGit: boolean }): WorkspaceContextV1 | undefined {
    if (!options.includeFile && !options.includeSelection && !options.includeGit) return undefined;
    const relative = input.filePath ? relativePath(input.workspaceRoot, input.filePath) : null;
    if (relative && sensitive(relative)) throw new Error("SENSITIVE_FILE_BLOCKED");
    const selection = options.includeSelection && input.selection && input.text ? bounded(input.text.slice(input.selection.start, input.selection.end)) : undefined;
    const selectedRange = selection && input.selection ? linesFor(input.text ?? "", input.selection.start, input.selection.end) : null;
    const excerpt = !selection && options.includeFile && input.text ? excerptAround(input.text, input.cursorLine ?? 0) : undefined;
    const payload: Omit<WorkspaceContextV1, "context_digest"> = { schema: WORKSPACE_CONTEXT_IDENTITY, workspace_name: input.workspaceName, workspace_id: digest(relative ?? input.workspaceName), active_file_relative_path: relative, language_id: input.languageId ?? null, selected_text: selection?.text ?? null, selected_text_range: selectedRange, active_file_excerpt: excerpt?.text ?? null, excerpt_range: excerpt?.range ?? null, git_branch: options.includeGit ? input.git?.branch ?? null : null, git_commit: options.includeGit ? input.git?.commit ?? null : null, git_dirty: options.includeGit ? input.git?.dirty ?? null : null, selection_truncated: selection?.truncated ?? false, excerpt_truncated: excerpt?.truncated ?? false };
    return { ...payload, context_digest: digest(payload) };
  }
}

function relativePath(root: string, file: string): string {
  const relative = path.relative(root, file).split(path.sep).join("/");
  if (!relative || relative.startsWith("../") || path.isAbsolute(relative)) throw new Error("FILE_OUTSIDE_WORKSPACE");
  return relative;
}
function sensitive(relative: string): boolean { const name = path.basename(relative).toLowerCase(); return name === ".env" || name.startsWith(".env.") || /(^credentials|^secrets|\.pem$|\.key$)/.test(name); }
function bounded(text: string): { text: string; truncated: boolean } { return { text: text.slice(0, MAX_CONTEXT_CHARACTERS), truncated: text.length > MAX_CONTEXT_CHARACTERS }; }
function excerptAround(text: string, cursor: number): { text: string; range: ContextRangeV1; truncated: boolean } { const lines = text.split("\n"); const start = Math.max(0, cursor - 40); const end = Math.min(lines.length, cursor + 41); const value = bounded(lines.slice(start, end).join("\n")); return { text: value.text, range: { start_line: start, end_line: end - 1 }, truncated: value.truncated || start > 0 || end < lines.length }; }
function linesFor(text: string, start: number, end: number): ContextRangeV1 { return { start_line: text.slice(0, start).split("\n").length - 1, end_line: text.slice(0, end).split("\n").length - 1 }; }
function digest(value: unknown): string { return createHash("sha256").update(JSON.stringify(value)).digest("hex"); }
