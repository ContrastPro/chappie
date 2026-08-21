export interface GitMetadataV1 { branch: string | null; commit: string; dirty: boolean; }
export type GitRunner = (args: readonly string[], cwd: string) => string;

const COMMIT = /^[0-9a-f]{40}$/;

export function collectGitMetadata(workspaceRoot: string, run: GitRunner): GitMetadataV1 | null {
  try {
    const branchValue = run(["rev-parse", "--abbrev-ref", "HEAD"], workspaceRoot).trim();
    const commit = run(["rev-parse", "HEAD"], workspaceRoot).trim().toLowerCase();
    const dirty = Boolean(run(["status", "--porcelain"], workspaceRoot).trim());
    if (!COMMIT.test(commit)) return null;
    return { branch: branchValue === "HEAD" || !branchValue ? null : branchValue, commit, dirty };
  } catch (_error) {
    return null;
  }
}
