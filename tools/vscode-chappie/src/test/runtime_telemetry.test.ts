import assert from "node:assert/strict";
import test from "node:test";
import { parseNvidiaSmiCsv, queryNvidiaTelemetry } from "../workspace/runtime_telemetry";

test("NVIDIA telemetry parses bounded machine-readable output and fails closed", async () => {
  const sample = parseNvidiaSmiCsv("NVIDIA RTX 4070 Ti, 46, 10445, 12282, 58\n", 100);
  assert.deepEqual(sample, { available: true, provider: "nvidia-smi", gpuName: "NVIDIA RTX 4070 Ti", utilizationPercent: 46, memoryUsedMb: 10445, memoryTotalMb: 12282, temperatureC: 58, sampledAtMs: 100 });
  assert.equal(parseNvidiaSmiCsv("bad").available, false);
  const unavailable = await queryNvidiaTelemetry((_file, _args, _options, callback) => callback(new Error("missing"), ""));
  assert.equal(unavailable.available, false);
});
