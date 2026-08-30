/* Grabs stills at chosen points in the replay, for verification and for sharing. */
import { spawn } from "node:child_process";
import { writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const PAGE = "file:///C:/Users/stock/AppData/Local/Temp/cst/page.html";
const W = 1280, H = 720, PORT = 9344;
const AT = [24, 96, 194];               // events elapsed when each still is taken
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const profile = mkdtempSync(join(tmpdir(), "cst-s-"));
const chrome = spawn(CHROME, [
  "--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  `--window-size=${W},${H}`, "--force-device-scale-factor=1", "--hide-scrollbars",
  "--allow-file-access-from-files", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--mute-audio", PAGE,
], { stdio: "ignore" });

let wsUrl;
for (let i = 0; i < 60 && !wsUrl; i++) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    wsUrl = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl)?.webSocketDebuggerUrl;
  } catch {}
  if (!wsUrl) await sleep(250);
}

const ws = new WebSocket(wsUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let nextId = 1; const pending = new Map();
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) {
    const { resolve, reject } = pending.get(m.id); pending.delete(m.id);
    m.error ? reject(new Error(m.error.message)) : resolve(m.result);
  }
};
const cdp = (method, params = {}) => new Promise((resolve, reject) => {
  const id = nextId++; pending.set(id, { resolve, reject });
  ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (e) => (await cdp("Runtime.evaluate", { expression: e, returnByValue: true })).result?.value;

await cdp("Page.enable"); await cdp("Runtime.enable");
await cdp("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });
await cdp("Page.navigate", { url: PAGE });
await sleep(1400);

await evaluate("window.__constellation.beginCapture()");
let done = 0;
for (const target of AT) {
  while (await evaluate(`window.__constellation.cursor`) < target &&
         !(await evaluate("window.__constellation.done"))) {
    await evaluate("window.__constellation.step(33.34)");
  }
  for (let i = 0; i < 26; i++) await evaluate("window.__constellation.step(33.34)"); // let it settle
  const { data } = await cdp("Page.captureScreenshot", { format: "png", fromSurface: true });
  const path = `C:\\Users\\stock\\AppData\\Local\\Temp\\cst\\still-${String(++done).padStart(2, "0")}.png`;
  writeFileSync(path, Buffer.from(data, "base64"));
  console.log("wrote", path, "at event", await evaluate("window.__constellation.cursor"));
}

ws.close(); chrome.kill(); await sleep(300); process.exit(0);
