const { writeFileSync } = require("node:fs");
const { writeHeapSnapshot } = require("node:v8");

const began = performance.now();
const window = Number(process.env.IBOARD_PROFILE_WINDOW_MS);
let captured = false;

function capture(at) {
  if (captured) return;
  captured = true;
  if (process.env.IBOARD_PROFILE_KIND === "heap") {
    writeHeapSnapshot("/artifacts/heap.heapsnapshot");
  }
  writeFileSync("/artifacts/capture-status.json",
    JSON.stringify({ at, elapsed_ms: Math.round(performance.now() - began) }));
}

process.once("exit", () => capture("exit"));
if (window > 0) {
  setTimeout(() => {
    capture("window");
    // End only this disposable diagnostic child so Node can flush the profile.
    process.exit(0);
  }, window).unref();
}
