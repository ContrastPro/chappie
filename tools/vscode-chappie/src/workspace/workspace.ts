import * as path from "node:path";

export type Platform = "win32" | "darwin" | "linux";
export type LocalWorkspaceState = "UNCONFIGURED" | "DEGRADED" | "READY";

export interface ChappieCommand { command: string; args: string[]; cwd: string; }
export interface SetupFailureSummary { readonly message: string; }
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

function pathFor(platform: Platform): typeof path.posix {
  return platform === "win32" ? path.win32 : path.posix;
}

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
  const platformPath = pathFor(platform);
  const executable = platform === "win32"
    ? platformPath.join(workspaceRoot, ".venv", "Scripts", "chappie.exe")
    : platformPath.join(workspaceRoot, ".venv", "bin", "chappie");
  return { command: exists(executable) ? executable : "chappie", args: ["serve"], cwd: workspaceRoot };
}

/** A local read-only hint for disconnected UX, never a Control Plane substitute. */
export function localWorkspaceState(
  workspaceRoot: string,
  platform: Platform,
  exists: (candidate: string) => boolean,
): LocalWorkspaceState {
  const executable = resolveChappieExecutable(workspaceRoot, platform, exists);
  if (executable.command === "chappie") { return "UNCONFIGURED"; }
  const platformPath = pathFor(platform);
  const config = platformPath.join(workspaceRoot, "data", "config", "chappie.json");
  const knowledge = platformPath.join(workspaceRoot, "data", "knowledge", "chappie_knowledge_v1.json");
  return exists(config) && exists(knowledge) ? "READY" : "DEGRADED";
}

/** Resolve only a workspace-local setup executable; never fall back to PATH. */
export function resolveChappieSetupCommand(
  workspaceRoot: string,
  platform: Platform,
  exists: (candidate: string) => boolean,
  resultFile?: string,
): ChappieCommand | undefined {
  const platformPath = pathFor(platform);
  const executable = platform === "win32"
    ? platformPath.join(workspaceRoot, ".venv", "Scripts", "chappie.exe")
    : platformPath.join(workspaceRoot, ".venv", "bin", "chappie");
  return exists(executable)
    ? { command: executable, args: ["setup", "--yes", "--interactive", ...(resultFile ? ["--result-file", resultFile] : [])], cwd: workspaceRoot }
    : undefined;
}

/** Decode only allow-listed canonical setup failures written by the CLI. */
export function readSetupFailureSummary(
  resultFile: string,
  readFile: (candidate: string, encoding: "utf8") => string,
): SetupFailureSummary | undefined {
  try {
    const value: unknown = JSON.parse(readFile(resultFile, "utf8"));
    if (!isRecord(value) || value.identity !== "chappie_setup_engine/1.0.0" || !Array.isArray(value.steps)) { return undefined; }
    const failed = value.steps.find((step): step is Record<string, unknown> => isRecord(step) && step.state === "FAILED");
    if (failed?.message === "KNOWLEDGE_BUILD_BLOCKED_DIRTY_REPOSITORY") {
      return { message: "Repair blocked: Knowledge requires a clean committed source snapshot. Commit or stash your changes, then retry Repair." };
    }
  } catch { /* Canonical terminal output remains the diagnostic fallback. */ }
  return undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
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

  public start(command: ChappieCommand, spawn: SpawnLike): SpawnedProcess | undefined {
    if (this.child) { return undefined; }
    this.child = spawn(command.command, command.args, { cwd: command.cwd, shell: false });
    return this.child;
  }

  public release(child: SpawnedProcess): void {
    if (this.child === child) { this.child = undefined; }
  }

  public stop(): boolean {
    if (!this.child) { return false; }
    this.child.kill();
    this.child = undefined;
    return true;
  }
}
