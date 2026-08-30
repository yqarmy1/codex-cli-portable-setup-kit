import { createHash, randomBytes } from "node:crypto";
import {
  closeSync,
  existsSync,
  fstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  readSync,
  renameSync,
  rmdirSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const LOCK_TIMEOUT_MS = 5_000;
const STALE_LOCK_MS = 60_000;
const sleepBuffer = new Int32Array(new SharedArrayBuffer(4));

function writeHookResult(result) {
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

function errorMessage(error) {
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(/[\r\n]+/g, " ").slice(0, 500);
}

function isInsideProject(projectRoot, cwd) {
  const relative = path.relative(projectRoot, cwd);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

function acquireLock(lockPath) {
  const deadline = Date.now() + LOCK_TIMEOUT_MS;
  while (true) {
    try {
      mkdirSync(lockPath);
      return;
    } catch (error) {
      if (error?.code !== "EEXIST") {
        throw error;
      }
      try {
        if (Date.now() - statSync(lockPath).mtimeMs > STALE_LOCK_MS) {
          rmdirSync(lockPath);
          continue;
        }
      } catch (statError) {
        if (statError?.code === "ENOENT") {
          continue;
        }
        throw statError;
      }
      if (Date.now() >= deadline) {
        throw new Error("Timed out while locking the compaction state.");
      }
      Atomics.wait(sleepBuffer, 0, 0, 100);
    }
  }
}

function writeJsonAtomically(filePath, value) {
  const directory = path.dirname(filePath);
  mkdirSync(directory, { recursive: true });
  const temporaryPath = path.join(
    directory,
    `.${path.basename(filePath)}.${process.pid}.${randomBytes(6).toString("hex")}.tmp`,
  );
  try {
    writeFileSync(temporaryPath, `${JSON.stringify(value, null, 2)}\n`, {
      encoding: "utf8",
      flag: "wx",
    });
    renameSync(temporaryPath, filePath);
  } finally {
    if (existsSync(temporaryPath)) {
      unlinkSync(temporaryPath);
    }
  }
}

function captureTranscriptCheckpoint(transcriptPath) {
  const descriptor = openSync(transcriptPath, "r");
  try {
    const byteLength = fstatSync(descriptor).size;
    const hash = createHash("sha256");
    const buffer = Buffer.allocUnsafe(64 * 1024);
    let offset = 0;
    while (offset < byteLength) {
      const requested = Math.min(buffer.length, byteLength - offset);
      const bytesRead = readSync(descriptor, buffer, 0, requested, offset);
      if (bytesRead <= 0) {
        throw new Error("Transcript ended before its captured byte boundary.");
      }
      hash.update(buffer.subarray(0, bytesRead));
      offset += bytesRead;
    }
    return {
      transcriptPath,
      byteLength,
      sha256: hash.digest("hex"),
    };
  } finally {
    closeSync(descriptor);
  }
}

try {
  const rawInput = readFileSync(0, "utf8").replace(/^\uFEFF/, "");
  const event = JSON.parse(rawInput);
  if (event.hook_event_name !== "PostCompact") {
    writeHookResult({ continue: true });
    process.exit(0);
  }

  const hookDirectory = path.dirname(fileURLToPath(import.meta.url));
  const projectRoot = path.resolve(hookDirectory, "..", "..");
  const eventCwd = path.resolve(String(event.cwd ?? ""));
  if (!isInsideProject(projectRoot, eventCwd)) {
    writeHookResult({ continue: true });
    process.exit(0);
  }

  const sessionId = String(event.session_id ?? "").trim();
  if (sessionId === "") {
    throw new Error("PostCompact did not include a session_id.");
  }

  const sessionKey = createHash("sha256").update(sessionId, "utf8").digest("hex");
  const sessionDirectory = path.join(
    projectRoot,
    ".context",
    "runtime",
    "codex-context-rollover",
    sessionKey,
  );
  mkdirSync(sessionDirectory, { recursive: true });
  const statePath = path.join(sessionDirectory, "state.json");
  const lockPath = path.join(sessionDirectory, "state.lock");

  acquireLock(lockPath);
  let compactionCount;
  let checkpoint;
  let checkpointError = null;
  try {
    let previousState = {};
    if (existsSync(statePath)) {
      previousState = JSON.parse(readFileSync(statePath, "utf8"));
    }
    const previousCount = Number.isInteger(previousState.compactionCount)
      ? previousState.compactionCount
      : 0;
    compactionCount = previousCount + 1;
    const recordedAtUtc = new Date().toISOString();

    try {
      const providedTranscriptPath = String(event.transcript_path ?? "").trim();
      if (providedTranscriptPath === "") {
        throw new Error("PostCompact did not include a transcript_path.");
      }
      const transcriptPath = path.resolve(eventCwd, providedTranscriptPath);
      checkpoint = captureTranscriptCheckpoint(transcriptPath);
      const checkpointRecord = {
        schemaVersion: 1,
        sessionKey,
        compactionCount,
        recordedAtUtc,
        trigger: String(event.trigger ?? ""),
        turnId: event.turn_id == null ? null : String(event.turn_id),
        cwd: eventCwd,
        ...checkpoint,
      };
      const timestamp = recordedAtUtc.replace(/[-:.]/g, "");
      const checkpointName = `${String(compactionCount).padStart(4, "0")}-${timestamp}-${randomBytes(4).toString("hex")}.json`;
      writeJsonAtomically(path.join(sessionDirectory, checkpointName), checkpointRecord);
    } catch (error) {
      checkpointError = errorMessage(error);
    }

    writeJsonAtomically(statePath, {
      schemaVersion: 1,
      sessionKey,
      compactionCount,
      updatedAtUtc: recordedAtUtc,
      lastCheckpoint: checkpoint ?? previousState.lastCheckpoint ?? null,
      checkpointError,
    });
  } finally {
    rmdirSync(lockPath);
  }

  if (compactionCount >= 2) {
    if (checkpoint) {
      const shortHash = checkpoint.sha256.slice(0, 12);
      writeHookResult({
        continue: true,
        systemMessage: `This task has completed compaction ${compactionCount}. A checkpoint was recorded (SHA-256 ${shortHash}, ${checkpoint.byteLength} bytes). To avoid context drift, use /new to open a clean task; the new task will not inherit the previous conversation automatically.`,
      });
    } else {
      writeHookResult({
        continue: true,
        systemMessage: `This task has completed compaction ${compactionCount}, but checkpoint recording failed: ${checkpointError}. Keep this task open and inspect the project's context-rollover runtime state.`,
      });
    }
  } else {
    writeHookResult({ continue: true });
  }
} catch (error) {
  writeHookResult({
    continue: true,
    systemMessage: `The Codex compaction checkpoint hook failed: ${errorMessage(error)}`,
  });
}
