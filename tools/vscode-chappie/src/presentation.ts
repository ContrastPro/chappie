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
  git_dirty: boolean | null;
} | undefined): string {
  if (!context) return "";
  const rows: string[] = [];
  if (context.active_file_relative_path) {
    const range = context.selected_text_range ?? context.excerpt_range;
    rows.push(range ? `${context.active_file_relative_path} · Lines ${range.start_line + 1}–${range.end_line + 1}` : context.active_file_relative_path);
  }
  if (context.git_branch) rows.push(`Git · ${context.git_branch}${context.git_dirty ? " · dirty" : ""}`);
  return rows.join("\n");
}

export function projectChatAvailability(
  connection: string,
  capability: string | undefined,
  aiProvider: string | null | undefined,
  knowledge: string,
): string | null {
  if (connection !== "CONNECTED") return "Chappie Control Plane is not connected.";
  if (capability === "AVAILABLE") return null;
  if (aiProvider === "NOT_CONFIGURED" && knowledge === "NOT_CONFIGURED") {
    return "Project chat unavailable. AI and Knowledge setup required.";
  }
  if (aiProvider === "NOT_CONFIGURED") {
    return "Project chat unavailable. Chappie AI is not configured for this workspace.";
  }
  if (knowledge === "NOT_CONFIGURED") {
    return "Project chat unavailable. Chappie Knowledge is not configured for this workspace.";
  }
  return "Project chat is temporarily unavailable. Refresh Chappie status for details.";
}

export function projectQaFailureMessage(reasonCode: string | undefined): string {
  if (reasonCode === "AI_UNAVAILABLE") return "Chappie AI is not configured for this workspace.";
  if (reasonCode === "KNOWLEDGE_UNAVAILABLE") return "Chappie Knowledge is not configured for this workspace.";
  return "Chappie Project QA is temporarily unavailable.";
}
