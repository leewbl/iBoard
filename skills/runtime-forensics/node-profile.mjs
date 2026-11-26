import { spawn } from "node:child_process";
import { mkdtemp, readFile, stat } from "node:fs/promises";
import { join } from "node:path";

const [kind, entry, loader, window, ...input] = process.argv.slice(2);
const filePattern = /^(?:[a-zA-Z0-9_.-]+\/)*[a-zA-Z0-9_.-]+\.(?:js|mjs|cjs|ts|mts|cts)$/;
const validFile = (path) => filePattern.test(path) && !path.split("/").some((part) => ["..", "."].includes(part));
if (!["cpu", "heap"].includes(kind) || !validFile(entry) || (loader && !validFile(loader)) ||
    !/^\d{1,5}$/.test(window) || Number(window) > 60_000) {
  throw new Error("Invalid Node profile request");
}

const home = await mkdtemp("/tmp/iboard-profile-");
const flags = [
  ...(kind === "cpu" ? ["--cpu-prof", "--cpu-prof-dir=/artifacts",
    "--cpu-prof-name=cpu.cpuprofile", "--cpu-prof-interval=2000"] : []),
  "--require", "/workspace/capture-node.cjs",
  ...(loader ? ["--import", "/app/" + loader] : []),
];
const result = await new Promise((resolve, reject) => {
  const child = spawn(process.execPath, [...flags, "/app/" + entry, ...input], {
    cwd: "/app",
    // Only the wrapper emits the machine-readable capture receipt on stdout.
    stdio: ["ignore", 2, 2],
    env: { ...process.env, HOME: home, OPENCLAW_HOME: home, OPENCLAW_STATE_DIR: join(home, "state"),
      XDG_CACHE_HOME: join(home, "cache"), IBOARD_PROFILE_KIND: kind, IBOARD_PROFILE_WINDOW_MS: window },
  });
  child.once("error", reject);
  child.once("close", (code, signal) => resolve({ code, signal }));
});

const artifact = kind === "cpu" ? "cpu.cpuprofile" : "heap.heapsnapshot";
const capture = JSON.parse(await readFile("/artifacts/capture-status.json", "utf8"));
if (!(await stat("/artifacts/" + artifact)).size || !["exit", "window"].includes(capture.at)) {
  throw new Error("The profiled Node process did not produce its requested capture");
}
console.log(JSON.stringify({ kind: "iboard-node-profile", profile: kind, target: result, capture }));
