#!/usr/bin/env node
// Simple onset/ending/pause timing diagnostic, not a listening check. Keep the height a power of two:
// arbitrary heights (e.g. 220) take a much slower path in the sandbox's FFmpeg 5.1.
// node spectrogram.mjs <audio> <out.png> [start_s=0] [window_s=10, max 30]

import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, renameSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";

const [input, output, startArg = "0", windowArg = "10", ...extra] = process.argv.slice(2);
const start = Number(startArg);
const window = Number(windowArg);
if (!input || !output || extra.length || !output.endsWith(".png") ||
    resolve(input) === resolve(output) || !Number.isFinite(start) || start < 0 ||
    !Number.isFinite(window) || window <= 0 || window > 30) {
  console.error("usage: spectrogram.mjs <audio> <out.png> [start_s>=0] [0<window_s<=30]");
  process.exit(2);
}

const target = resolve(output);
const temporary = `${target}.${randomUUID()}.png`;
try {
  mkdirSync(dirname(target), { recursive: true });
  const result = spawnSync("ffmpeg", [
    "-nostdin", "-v", "error", "-threads", "1", "-filter_complex_threads", "1",
    "-ss", String(start), "-t", String(window), "-i", resolve(input),
    "-lavfi", "aformat=channel_layouts=mono,showspectrumpic=s=1024x256:legend=1:scale=log:fscale=log",
    "-frames:v", "1", "-y", temporary,
  ], { encoding: "utf8", timeout: 10000, killSignal: "SIGKILL", maxBuffer: 1024 * 1024 });
  if (result.error || result.status !== 0) {
    throw new Error(result.error?.code === "ETIMEDOUT"
      ? "spectrogram exceeded 10 seconds; skip this optional diagnostic"
      : `spectrogram failed: ${result.error?.message || result.stderr.trim() || result.signal || result.status}`);
  }
  renameSync(temporary, target);
  console.log(JSON.stringify({
    ok: true, file: target, start_s: start, window_s: window,
    note: "Timing checks only: onset, ending, pauses. Human audition and confirmation are required before production use.",
  }));
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
} finally {
  rmSync(temporary, { force: true });
}
