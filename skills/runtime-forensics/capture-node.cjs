const { copyFileSync, mkdtempSync, renameSync, rmSync, statSync, writeFileSync } = require("node:fs");
const { writeHeapSnapshot } = require("node:v8");

const began = performance.now();
const kind = process.env.IBOARD_PROFILE_KIND;
const window = Number(process.env.IBOARD_PROFILE_WINDOW_MS);
const heapPath = "/artifacts/heap.heapsnapshot";
const maxHeapBytes = 64 * 1024 * 1024;
let captured = false;

function captureAllocations() {
  // Keep incomplete snapshots in the sandbox, including when the child is killed.
  const tempDirectory = mkdtempSync("/tmp/iboard-heap-");
  const snapshot = tempDirectory + "/heap.heapsnapshot";
  const partial = heapPath + ".part";
  try {
    writeHeapSnapshot(snapshot);
    const size = statSync(snapshot).size;
    if (size === 0 || size > maxHeapBytes) {
      throw new Error("Allocation heap snapshot is empty or exceeds 64 MiB");
    }
    copyFileSync(snapshot, partial);
    renameSync(partial, heapPath);
  } finally {
    rmSync(partial, { force: true });
    rmSync(tempDirectory, { recursive: true, force: true });
  }
}

function capture(at) {
  if (captured) return;
  captured = true;
  if (kind === "heap") {
    writeHeapSnapshot(heapPath);
  } else if (kind === "heap-allocations") {
    captureAllocations();
  }
  writeFileSync("/artifacts/capture-status.json",
    JSON.stringify({ at, elapsed_ms: Math.round(performance.now() - began),
      ...(kind === "heap-allocations" ? { allocation_tracking: true } : {}) }));
}

process.once("exit", () => capture("exit"));
if (window > 0) {
  setTimeout(() => {
    capture("window");
    // End only this disposable diagnostic child so Node can flush the profile.
    process.exit(0);
  }, window).unref();
}
