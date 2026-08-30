/*
 * Records the constellation UI filling in, as an MP4.
 *
 * Drives a real headless Chrome over the DevTools Protocol and steps the page's
 * replay on a fixed dt, so frames are evenly spaced no matter how slow the
 * capture round-trip is. Frames go straight into a WASM H.264 encoder — no
 * ffmpeg, which matters here because this is win32-arm64.
 */

import { spawn } from "node:child_process";
import { writeFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import HME from "h264-mp4-encoder";
import { PNG } from "pngjs";

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const PAGE = "file:///C:/Users/stock/AppData/Local/Temp/cst/page.html";
const OUT = "C:\\Users\\stock\\AppData\\Local\\Temp\\cst\\constellation-load.mp4";

const W = 1280, H = 720, FPS = 30;
const STEPS_PER_FRAME = 3;          // replay runs at 3x, output plays at 30fps
const DT = 1000 / FPS;              // one physics step per rendered-frame's worth of time
const LEAD = 12, TAIL = 50, MAX_FRAMES = 900;
const PORT = 9333;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------- chrome

const profile = mkdtempSync(join(tmpdir(), "cst-"));
const chrome = spawn(CHROME, [
  "--headless=new",
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`,
  `--window-size=${W},${H}`,
  "--force-device-scale-factor=1",
  "--hide-scrollbars",
  "--allow-file-access-from-files",
  "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--mute-audio",
  "--disable-features=Translate,MediaRouter",
  PAGE,
], { stdio: "ignore" });

async function findTarget() {
  for (let i = 0; i < 60; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (page) return page.webSocketDebuggerUrl;
    } catch { /* not up yet */ }
    await sleep(250);
  }
  throw new Error("Chrome never exposed a debuggable page target");
}

const wsUrl = await findTarget();
const ws = new WebSocket(wsUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

let nextId = 1;
const pending = new Map();
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
  }
};
const cdp = (method, params = {}) => new Promise((resolve, reject) => {
  const id = nextId++;
  pending.set(id, { resolve, reject });
  ws.send(JSON.stringify({ id, method, params }));
});

const evaluate = async (expression) => {
  const r = await cdp("Runtime.evaluate", { expression, returnByValue: true });
  return r.result?.value;
};

// ---------------------------------------------------------------- capture

await cdp("Page.enable");
await cdp("Runtime.enable");
await cdp("Emulation.setDeviceMetricsOverride",
  { width: W, height: H, deviceScaleFactor: 1, mobile: false });

// the page may already have loaded before we attached
await cdp("Page.navigate", { url: PAGE });
await sleep(1400);

const ready = await evaluate("typeof window.__constellation === 'object'");
if (!ready) throw new Error("the page never exposed __constellation");

const encoder = await HME.createH264MP4Encoder();
encoder.width = W;
encoder.height = H;
encoder.frameRate = FPS;
encoder.quantizationParameter = 20;     // visually lossless-ish on a dark field
encoder.speed = 4;
encoder.initialize();

async function grab() {
  const { data } = await cdp("Page.captureScreenshot", { format: "png", fromSurface: true });
  const png = PNG.sync.read(Buffer.from(data, "base64"));
  if (png.width !== W || png.height !== H) {
    throw new Error(`frame was ${png.width}x${png.height}, expected ${W}x${H}`);
  }
  encoder.addFrameRgba(new Uint8Array(png.data.buffer, png.data.byteOffset, png.data.length));
}

await evaluate("window.__constellation.beginCapture()");

let frames = 0;
for (let i = 0; i < LEAD; i++) { await evaluate("window.__constellation.step(3)"); await grab(); frames++; }

const advance = `for(let i=0;i<${STEPS_PER_FRAME};i++)window.__constellation.step(${DT});` +
                `({done:window.__constellation.done,cursor:window.__constellation.cursor})`;

let state = { done: false, cursor: 0 };
while (!state.done && frames < MAX_FRAMES) {
  state = await evaluate(advance);
  await grab();
  if (++frames % 30 === 0) {
    process.stdout.write(`  ${frames} frames · event ${state.cursor}/${await evaluate("window.__constellation.total")}\n`);
  }
}

for (let i = 0; i < TAIL; i++) { await evaluate("window.__constellation.step(33.34)"); await grab(); frames++; }

encoder.finalize();
writeFileSync(OUT, Buffer.from(encoder.FS.readFile(encoder.outputFilename)));
encoder.delete();

console.log(`\nwrote ${OUT}`);
console.log(`${frames} frames · ${(frames / FPS).toFixed(1)}s · ${W}x${H} @ ${FPS}fps`);

ws.close();
chrome.kill();
await sleep(400);
try { rmSync(profile, { recursive: true, force: true }); } catch { /* chrome still holding it */ }
process.exit(0);
