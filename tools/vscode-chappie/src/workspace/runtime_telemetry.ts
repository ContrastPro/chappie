import { execFile } from "node:child_process";

export interface RuntimeTelemetryV1 { available: boolean; provider: "nvidia-smi" | null; gpuName: string | null; utilizationPercent: number | null; memoryUsedMb: number | null; memoryTotalMb: number | null; temperatureC: number | null; sampledAtMs: number; }
export type ExecFileLike = (file: string, args: readonly string[], options: { timeout: number; windowsHide: boolean; shell: false }, callback: (error: Error | null, stdout: string) => void) => void;
const unavailable = (): RuntimeTelemetryV1 => ({ available: false, provider: null, gpuName: null, utilizationPercent: null, memoryUsedMb: null, memoryTotalMb: null, temperatureC: null, sampledAtMs: Date.now() });
export function parseNvidiaSmiCsv(stdout: string, now = Date.now()): RuntimeTelemetryV1 {
  const values = stdout.trim().split("\n")[0]?.split(",").map((item) => item.trim()) ?? [];
  if (values.length !== 5) return unavailable();
  const [gpuName, utilization, used, total, temperature] = values; const numeric = [utilization, used, total, temperature].map(Number);
  if (!gpuName || numeric.some((value) => !Number.isFinite(value) || value < 0)) return unavailable();
  return { available: true, provider: "nvidia-smi", gpuName, utilizationPercent: numeric[0], memoryUsedMb: numeric[1], memoryTotalMb: numeric[2], temperatureC: numeric[3], sampledAtMs: now };
}
export function queryNvidiaTelemetry(run: ExecFileLike = defaultExec): Promise<RuntimeTelemetryV1> {
  return new Promise((resolve) => run("nvidia-smi", ["--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"], { timeout: 1_000, windowsHide: true, shell: false }, (error, stdout) => resolve(error ? unavailable() : parseNvidiaSmiCsv(stdout))));
}
function defaultExec(file: string, args: readonly string[], options: { timeout: number; windowsHide: boolean; shell: false }, callback: (error: Error | null, stdout: string) => void): void { execFile(file, [...args], options, (error, stdout) => callback(error, String(stdout))); }
