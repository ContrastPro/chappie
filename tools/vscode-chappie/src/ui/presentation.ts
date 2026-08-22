export function capabilitySummary(capabilities: readonly { status: string }[]): string {
  return `${capabilities.filter((item) => item.status === "AVAILABLE").length}/${capabilities.length}`;
}

export function humanState(value: string | null | undefined): string {
  const labels: Record<string, string> = {
    AVAILABLE: "Available",
    DEGRADED: "Needs attention",
    NOT_CONFIGURED: "Not configured",
    NOT_INSTALLED: "Not installed",
    SETUP_REQUIRED: "Setup required",
    READY: "Ready",
    UNKNOWN: "Unknown",
    UNAVAILABLE: "Unavailable",
    CONNECTED: "Connected",
    DISCONNECTED: "Disconnected",
    INCOMPATIBLE: "Incompatible",
  };
  return labels[value ?? "UNKNOWN"] ?? (value ? value.replaceAll("_", " ").toLowerCase() : "Unknown");
}

export function contextPreview(context: {
  active_file_relative_path: string | null;
  selected_text_range: { start_line: number; end_line: number } | null;
  excerpt_range: { start_line: number; end_line: number } | null;
  git_branch: string | null;
  git_commit?: string | null;
  git_dirty: boolean | null;
} | undefined): string {
  if (!context) return "";
  const rows: string[] = [];
  if (context.active_file_relative_path) {
    const range = context.selected_text_range ?? context.excerpt_range;
    rows.push(range ? `${context.active_file_relative_path} · Lines ${range.start_line + 1}–${range.end_line + 1}` : context.active_file_relative_path);
  }
  if (context.git_branch || context.git_commit) rows.push(`Git · ${context.git_branch ?? "detached"}${context.git_commit ? ` · ${context.git_commit.slice(0, 12)}` : ""} · ${context.git_dirty ? "dirty" : "clean"}`);
  return rows.join("\n");
}

export function projectChatAvailability(
  connection: string,
  capability: string | undefined,
  aiProvider: string | null | undefined,
  knowledge: string,
  localState: "UNCONFIGURED" | "DEGRADED" | "READY" = "UNCONFIGURED",
): string | null {
  if (connection !== "CONNECTED") {
    if (localState === "READY") return "Chappie is ready, but the local service is disconnected.";
    if (localState === "DEGRADED") return "Chappie is configured but needs repair before the local service can connect.";
    return "Chappie setup is required before the local service can connect.";
  }
  // A capability advertisement is not sufficient on its own: Project QA is
  // grounded in the current Knowledge snapshot and must never start against
  // stale Knowledge.
  if (aiProvider === "NOT_CONFIGURED" && knowledge === "NOT_CONFIGURED") {
    return "Project chat unavailable. AI and Knowledge setup required.";
  }
  if (aiProvider === "NOT_CONFIGURED") {
    return "Project chat unavailable. Chappie AI is not configured for this workspace.";
  }
  if (knowledge === "NOT_CONFIGURED") {
    return "Project chat unavailable. Chappie Knowledge is not configured for this workspace.";
  }
  if (knowledge === "STALE") {
    return "Knowledge is stale. Repair Chappie to use Project QA.";
  }
  if (knowledge !== "READY") {
    return "Project chat unavailable. Knowledge is not ready for grounded answers.";
  }
  if (aiProvider !== "READY") {
    return "Project chat unavailable. Chappie AI is not ready for this workspace.";
  }
  if (capability === "AVAILABLE") return null;
  return "Project chat is temporarily unavailable. Refresh Chappie status for details.";
}

/** Use repair language once any configured Chappie state can be restored. */
export function setupActionLabel(
  connection: string,
  capability: string | undefined,
  aiProvider: string | null | undefined,
  knowledge: string,
  localState: "UNCONFIGURED" | "DEGRADED" | "READY" = "UNCONFIGURED",
): "Set up" | "Repair" | "Reconnect" {
  if (connection !== "CONNECTED") {
    return localState === "READY" ? "Reconnect" : localState === "DEGRADED" ? "Repair" : "Set up";
  }
  if (capability === "AVAILABLE" && aiProvider === "READY" && knowledge === "READY") return "Set up";
  return aiProvider === "NOT_CONFIGURED" && knowledge === "NOT_CONFIGURED" ? "Set up" : "Repair";
}

export function projectQaFailureMessage(reasonCode: string | undefined): string {
  if (reasonCode === "AI_UNAVAILABLE") return "Chappie AI is not configured for this workspace.";
  if (reasonCode === "KNOWLEDGE_UNAVAILABLE") return "Chappie Knowledge is not configured for this workspace.";
  if (reasonCode === "AI_TIMEOUT") return "Chappie Project QA timed out before local inference completed.";
  return "Chappie Project QA is temporarily unavailable.";
}
