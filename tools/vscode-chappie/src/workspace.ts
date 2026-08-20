import * as path from "node:path";

export type Platform = "win32" | "darwin" | "linux";

export interface ChappieCommand { command: string; args: string[]; cwd: string; }
export interface SpawnedProcess { kill(signal?: NodeJS.Signals): boolean; }
export type SpawnLike = (command: string, args: readonly string[], options: { cwd: string; shell: false }) => SpawnedProcess;
export interface SetupTerminalV1 {
  readonly name: string;
  readonly exitStatus?: { readonly code?: number };
  show(preserveFocus?: boolean): void;
}
export interface SetupTerminalOptionsV1 {
  name: "Chappie Setup";
  cwd: string;
  shellPath: string;
  shellArgs: string[];
}
export type CreateSetupTerminalV1 = (options: SetupTerminalOptionsV1) => SetupTerminalV1;

export function isChappieWorkspace(
  workspaceRoot: string,
  exists: (candidate: string) => boolean,
  isDirectory: (candidate: string) => boolean,
): boolean {
  return exists(path.join(workspaceRoot, "pyproject.toml"))
    && isDirectory(path.join(workspaceRoot, "app"))
    && exists(path.join(workspaceRoot, "app", "cli.py"));
}

export function shouldAutoStartControlPlane(
  workspaceRecognized: boolean,
  autoStartEnabled: boolean,
  connectionState: string,
): boolean {
  return workspaceRecognized && autoStartEnabled && connectionState === "DISCONNECTED";
}

export function resolveChappieExecutable(
  workspaceRoot: string,
  platform: Platform,
  exists: (candidate: string) => boolean,
): ChappieCommand {
  const executable = platform === "win32"
    ? path.join(workspaceRoot, ".venv", "Scripts", "chappie.exe")
    : path.join(workspaceRoot, ".venv", "bin", "chappie");
  return { command: exists(executable) ? executable : "chappie", args: ["serve"], cwd: workspaceRoot };
}

/** Resolve only a workspace-local setup executable; never fall back to PATH. */
export function resolveChappieSetupCommand(
  workspaceRoot: string,
  platform: Platform,
  exists: (candidate: string) => boolean,
): ChappieCommand | undefined {
  const executable = platform === "win32"
    ? path.join(workspaceRoot, ".venv", "Scripts", "chappie.exe")
    : path.join(workspaceRoot, ".venv", "bin", "chappie");
  return exists(executable)
    ? { command: executable, args: ["setup", "--yes", "--interactive"], cwd: workspaceRoot }
    : undefined;
}

export function setupBootstrapRemediation(platform: Platform): string {
  return platform === "win32"
    ? "Chappie environment is not bootstrapped. Run .\\setup.ps1."
    : "Chappie environment is not bootstrapped. Run ./setup.sh.";
}

/** Holds one interactive setup terminal and reports only its observed exit result. */
export class ChappieSetupTerminalV1 {
  private terminal: SetupTerminalV1 | undefined;

  public get running(): boolean { return this.terminal !== undefined; }

  public launch(command: ChappieCommand, create: CreateSetupTerminalV1): "STARTED" | "REUSED" {
    if (this.terminal) {
      this.terminal.show(true);
      return "REUSED";
    }
    this.terminal = create({
      name: "Chappie Setup",
      cwd: command.cwd,
      shellPath: command.command,
      shellArgs: command.args,
    });
    this.terminal.show(true);
    return "STARTED";
  }

  public adopt(terminal: SetupTerminalV1): void {
    this.terminal = terminal;
    terminal.show(true);
  }

  public closed(
    terminal: SetupTerminalV1,
    onSuccess: () => void,
    onFailure: () => void,
  ): void {
    if (terminal !== this.terminal) { return; }
    this.terminal = undefined;
    if (terminal.exitStatus?.code === 0) { onSuccess(); } else { onFailure(); }
  }
}

export class OwnedControlPlaneProcess {
  private child: SpawnedProcess | undefined;

  public get running(): boolean { return this.child !== undefined; }

  public start(command: ChappieCommand, spawn: SpawnLike): void {
    if (this.child) { return; }
    this.child = spawn(command.command, command.args, { cwd: command.cwd, shell: false });
  }

  public stop(): boolean {
    if (!this.child) { return false; }
    this.child.kill();
    this.child = undefined;
    return true;
  }
}
