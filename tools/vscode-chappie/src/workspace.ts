import * as path from "node:path";

export type Platform = "win32" | "darwin" | "linux";

export interface ChappieCommand { command: string; args: string[]; cwd: string; }
export interface SpawnedProcess { kill(signal?: NodeJS.Signals): boolean; }
export type SpawnLike = (command: string, args: readonly string[], options: { cwd: string; shell: false }) => SpawnedProcess;

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
