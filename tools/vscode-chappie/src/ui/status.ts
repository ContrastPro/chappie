import * as vscode from "vscode";
import { ConnectionState, ControlPlaneAIStatusV1, ControlPlaneCapabilitiesV1, ControlPlaneStatusV1 } from "../api/contracts";
import { capabilitySummary, humanState } from "./presentation";

export interface StatusSnapshotV1 {
  status?: ControlPlaneStatusV1;
  ai?: ControlPlaneAIStatusV1;
  capabilities?: ControlPlaneCapabilitiesV1;
}

export class ChappieStatusTreeV1 implements vscode.TreeDataProvider<vscode.TreeItem> {
  private readonly changed = new vscode.EventEmitter<void>();
  public readonly onDidChangeTreeData = this.changed.event;
  private state: ConnectionState = "DISCONNECTED";
  private snapshot: StatusSnapshotV1 = {};

  public update(state: ConnectionState, snapshot: StatusSnapshotV1 = {}): void {
    this.state = state;
    this.snapshot = snapshot;
    this.changed.fire();
  }

  public getTreeItem(item: vscode.TreeItem): vscode.TreeItem { return item; }

  public getChildren(item?: vscode.TreeItem): vscode.TreeItem[] {
    if (item) return [];
    const rows = [
      `* ${humanState(this.state)}`,
      `System: ${humanState(String(this.snapshot.status?.status?.overall_state ?? "UNKNOWN"))}`,
      `AI: ${this.snapshot.ai?.model ?? humanState(this.snapshot.ai?.provider_state)}`,
      `Knowledge: ${componentState(this.snapshot.status, "KNOWLEDGE")}`,
      `Capabilities: ${capabilitySummary(this.snapshot.capabilities?.capabilities ?? [])}`,
      "Open Chappie Workbench",
    ];
    return rows.map((label) => {
      const result = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
      if (label.startsWith("Capabilities:")) result.command = { command: "chappie.showCapabilities", title: "Chappie: Show Capabilities" };
      if (label === "Open Chappie Workbench") result.command = { command: "chappie.openWorkbench", title: "Chappie: Open Workbench" };
      if (label === "System: SETUP_REQUIRED") result.command = { command: "chappie.runSetup", title: "Chappie: Set Up" };
      return result;
    });
  }
}

function componentState(status: ControlPlaneStatusV1 | undefined, component: string): string {
  const components = Array.isArray(status?.status?.components) ? status.status.components : [];
  const found = components.find((item) => typeof item === "object" && item !== null && (item as { component?: string }).component === component) as { state?: string } | undefined;
  return humanState(found?.state);
}
