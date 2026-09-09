/* Performance sampling against the REAL service stack:
 * docs/aresclaw-dashboard/design.md covers capacity/deployment boundaries;
 * docs/aresclaw-dashboard/progress.md records measured validation evidence.
 * publishes 1/5/10 MiB HTML via the integration CLI, then measures in
 * headless Chromium: launcher→loader hop, /content backend wait (network
 * timings), first paint of the sandboxed frame, first interaction latency,
 * and JS heap. Prints one JSON report; not part of the unit test run.
 *
 * Usage: node web/tests/perf-sample.mjs
 * Requires the dev MySQL (13306) and MinIO (19000) to be reachable and a
 * Chromium at DASHBOARD_TEST_BROWSER (or the default lookup).
 */
import { spawn, spawnSync } from "node:child_process"
import { existsSync } from "node:fs"
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises"
import path from "node:path"
import { fileURLToPath } from "node:url"

const svcRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  ".."
)
const py = path.join(svcRoot, ".venv", "Scripts", "python.exe")
const browserPath =
  process.env.DASHBOARD_TEST_BROWSER ||
  [
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  ].find(existsSync)
if (!browserPath) throw new Error("no Chromium found (DASHBOARD_TEST_BROWSER)")

const CONTROL_PORT = 18180
const CONTENT_PORT = 18181
const W3_PORT = 18190
const DB = "aresclaw_dash_fresh"
const BUCKET = "aresclaw-dash-impltest"
const PREFIX = "perf/"
const env = {
  ...process.env,
  DASHBOARD_DATABASE_URL: `mysql+pymysql://dash:dash-test-pw-13306@127.0.0.1:13306/${DB}`,
  DASHBOARD_JWT_KEY_FILE: path.join(svcRoot, "devtools", "jwt-perf.key"),
  DASHBOARD_STORAGE_DIR: path.join(svcRoot, "data", "perf-staging"),
  DASHBOARD_CONTROL_ORIGIN: `http://127.0.0.1:${CONTROL_PORT}`,
  DASHBOARD_CONTENT_ORIGIN: `http://127.0.0.1:${CONTENT_PORT}`,
  DASHBOARD_S3_ENDPOINT_URL: "http://127.0.0.1:19000",
  DASHBOARD_S3_BUCKET: BUCKET,
  DASHBOARD_S3_PREFIX: PREFIX,
  DASHBOARD_W3_VERIFY_URL: `http://127.0.0.1:${W3_PORT}/verify`,
  DASHBOARD_DEV_LOGIN: "1",
  AWS_ACCESS_KEY_ID: "minioadmin",
  AWS_SECRET_ACCESS_KEY: "minioadmin",
  AWS_DEFAULT_REGION: "us-east-1",
  PYTHONIOENCODING: "utf-8",
}

const delay = (ms) => new Promise((r) => setTimeout(r, ms))
const waitReady = async (url, timeout = 30000) => {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url)
      if (response.ok) return
    } catch {}
    await delay(250)
  }
  throw new Error(`service not ready: ${url}`)
}

function sampleHtml(mib) {
  const target = mib * 1024 * 1024
  const head =
    '<!doctype html><meta charset="utf-8"><style>.row{padding:1px}.cell{display:inline-block;width:8px;height:8px}</style>' +
    '<button id="tap" onclick="ping()">点我</button>'
  const script =
    '<script>window.addEventListener("load",()=>{' +
    'const out=document.getElementById("grid");' +
    'for(let i=0;i<3000;i++){const d=document.createElement("div");' +
    'd.className="cell";d.textContent="c"+i;out.append(d)}' +
    "window.__cellsBuilt=true});" +
    'function ping(){parent.postMessage("interaction-complete","*")}<\/script>' +
    '<div id="grid"></div>'
  const block = '<div class="row"><span class="k">metric</span><span class="v">128</span></div>'
  const overhead = head.length + script.length + block.length
  const bodyBlocks = Math.max(
    1,
    Math.floor((target - overhead) / block.length)
  )
  return head + block.repeat(bodyBlocks) + script
}

// ---------------------------------------------------------------- stack
await writeFile(
  path.join(svcRoot, "devtools", "jwt-perf.key"),
  "perf-sample-key-material-0123456789abcdef0123456789abcdef"
)
const procs = []
const start = (args) => {
  const child = spawn(py, args, {
    cwd: svcRoot,
    env,
    windowsHide: true,
    stdio: "ignore",
  })
  procs.push(child)
  return child
}
start([
  "-m",
  "dashboard_service",
  "serve-control",
  "--port",
  String(CONTROL_PORT),
])
start([
  "-m",
  "dashboard_service",
  "serve-content",
  "--port",
  String(CONTENT_PORT),
])
start(["devtools/dev_w3_stub.py", String(W3_PORT)])
await waitReady(`http://127.0.0.1:${CONTROL_PORT}/health/ready`)
await waitReady(`http://127.0.0.1:${CONTENT_PORT}/health/live`)

// ------------------------------------------------------- account + publish
const op = (args) =>
  spawnSync(py, ["-m", "dashboard_service.operator", ...args], {
    cwd: svcRoot,
    env,
    encoding: "utf-8",
  })
const created = op([
  "create-account",
  "--account",
  "perf-machine",
  "--scopes",
  "read,write,manage",
])
if (created.status !== 0 && !/already exists/.test(created.stderr || ""))
  throw new Error(`create-account failed: ${created.stderr}`)
const issued = JSON.parse(op(["issue", "--account", "perf-machine"]).stdout)

const work = await mkdtemp(path.join(svcRoot, "data", ".perf-"))
const configPath = path.join(work, "config.json")
await writeFile(
  configPath,
  JSON.stringify({
    service_url: `http://127.0.0.1:${CONTROL_PORT}`,
    workdir: work,
    token_file: path.join(work, "token.txt"),
    timeout_seconds: 120,
  })
)
await writeFile(path.join(work, "token.txt"), issued.token + "\n")

const cli = (args) =>
  spawnSync(
    py,
    [
      path.join(
        svcRoot,
        "..",
        "..",
        "integrations",
        "aresclaw-dashboard",
        "scripts",
        "dashboard_cli.py"
      ),
      "--auth-mode",
      "integration",
      "--config",
      configPath,
      ...args,
    ],
    { cwd: work, encoding: "utf-8", env }
  )
const { randomUUID } = await import("node:crypto")

// The dev browser user needs viewer access to the private samples; resolve
// the stable principal first so the grant targets the right subject.
const devMe = await (
  await fetch(`http://127.0.0.1:${CONTROL_PORT}/api/v1/me`, {
    headers: { Authorization: "Bearer dev-alice", "X-Dashboard-Auth-Mode": "human" },
  })
).json()
if (!devMe.principal_id) throw new Error("dev W3 stub did not resolve dev-alice")

const boards = []
for (const mib of [1, 5, 10]) {
  const file = path.join(work, `sample-${mib}.html`)
  const html = sampleHtml(mib)
  await writeFile(file, html)
  const t0 = Date.now()
  const result = cli([
    "publish",
    "--file",
    `sample-${mib}.html`,
    "--title",
    `性能样本 ${mib}MiB`,
    "--request-id",
    randomUUID(),
  ])
  const publishMs = Date.now() - t0
  if (result.status !== 0)
    throw new Error(`publish ${mib}MiB failed: ${result.stdout}`)
  const dashboard_id = JSON.parse(result.stdout).result.dashboard_id
  const granted = cli([
    "share",
    dashboard_id,
    "--subject-type",
    "user",
    "--subject-id",
    devMe.principal_id,
    "--role",
    "viewer",
    "--expected-revision",
    "1",
    "--request-id",
    randomUUID(),
  ])
  if (granted.status !== 0)
    throw new Error(`grant ${mib}MiB failed: ${granted.stdout}`)
  boards.push({ mib, bytes: html.length, dashboard_id, publishMs })
}

// ------------------------------------------------------------- browser
const profile = await mkdtemp(path.join(svcRoot, "data", ".perf-profile-"))
const chrome = spawn(
  browserPath,
  [
    "--headless=new",
    "--no-first-run",
    "--disable-gpu",
    "--remote-debugging-port=0",
    `--user-data-dir=${profile}`,
    "about:blank",
  ],
  { windowsHide: true, stdio: "ignore" }
)
let port
for (let i = 0; i < 100; i++) {
  try {
    port = (
      await readFile(path.join(profile, "DevToolsActivePort"), "utf8")
    ).split("\n")[0]
    break
  } catch {
    await delay(100)
  }
}
if (!port) throw new Error("chromium did not start")
const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()

let sequence = 0
const waiting = new Map()
const events = []
const cdp = new WebSocket(
  pages.find((page) => page.type === "page").webSocketDebuggerUrl
)
cdp.addEventListener("message", ({ data }) => {
  const message = JSON.parse(data)
  for (const name of [
    "Network.requestWillBeSent",
    "Network.responseReceived",
    "Network.loadingFinished",
  ]) {
    if (message.method === name)
      events.push({ name, ...message.params, at: Date.now() })
  }
  const pending = waiting.get(message.id)
  if (!pending) return
  waiting.delete(message.id)
  if (message.error) pending.reject(new Error(message.error.message))
  else pending.resolve(message.result)
})
await new Promise((r) => cdp.addEventListener("open", r, { once: true }))
const call = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const id = ++sequence
    waiting.set(id, { resolve, reject })
    cdp.send(JSON.stringify({ id, method, params }))
  })
const evaluate = async (expression) =>
  (
    await call("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    })
  ).result.value
await call("Page.enable")
await call("Runtime.enable")
await call("Network.enable")
await call("Page.addScriptToEvaluateOnNewDocument", {
  source:
    'window.addEventListener("message", e => { (window.perfMessages ||= []).push({data: e.data, at: performance.now()}) })',
})

const report = []
for (const board of boards) {
  events.length = 0
  // Dev login state for the launcher (dev adapter reads sessionStorage).
  await call("Page.navigate", { url: `http://127.0.0.1:${CONTROL_PORT}/` })
  await delay(300)
  await evaluate('sessionStorage.setItem("dashboard-dev-token", "dev-alice")')
  await evaluate("window.perfMessages = []")
  const t0 = Date.now()
  await call("Page.navigate", {
    url: `http://127.0.0.1:${CONTROL_PORT}/dashboards/${board.dashboard_id}`,
  })
  // Wait until the single-layer flow finished inside the content origin.
  const deadline = Date.now() + 60000
  let done = false
  while (Date.now() < deadline) {
    done = await evaluate(
      'location.pathname.startsWith("/view/") && document.querySelector("#dashboard-content") !== null'
    )
    if (done) break
    await delay(30)
  }
  if (!done) throw new Error(`view did not finish for ${board.mib}MiB`)
  const totalMs = Date.now() - t0
  const content = events.filter(
    (e) =>
      e.name === "Network.requestWillBeSent" &&
      e.request &&
      e.request.url.endsWith("/content")
  )[0]
  const contentDone = events.filter(
    (e) =>
      e.name === "Network.loadingFinished" &&
      e.requestId &&
      content &&
      e.requestId === content.requestId
  )[0]
  const backendMs =
    contentDone && content ? Math.max(0, contentDone.at - content.at) : null
  // First interaction: click the sandboxed button and wait for the message.
  await evaluate("window.perfMessages = []")
  const click0 = Date.now()
  for (let i = 0; i < 40; i++) {
    await call("Input.dispatchMouseEvent", {
      type: "mousePressed",
      x: 60,
      y: 20,
      button: "left",
      clickCount: 1,
    })
    await call("Input.dispatchMouseEvent", {
      type: "mouseReleased",
      x: 60,
      y: 20,
      button: "left",
      clickCount: 1,
    })
    if (await evaluate("(window.perfMessages||[]).length > 0")) break
    await delay(120)
  }
  const interactMs = Date.now() - click0
  const heap = await evaluate(
    "performance.memory ? Math.round(performance.memory.usedJSHeapSize/1048576*10)/10 : null"
  )
  const iframeCount = await evaluate(
    "document.querySelectorAll('iframe').length"
  )
  report.push({
    sample: `${board.mib}MiB`,
    bytes: board.bytes,
    publish_ms: board.publishMs,
    view_total_ms: totalMs,
    content_backend_wait_ms: backendMs,
    first_interaction_ms: interactMs,
    loader_heap_mib: heap,
    iframes: iframeCount,
  })
}

console.log(
  JSON.stringify(
    {
      environment: {
        chromium: true,
        note: "headless Chromium via CDP; backend = real FastAPI + MySQL + MinIO",
      },
      samples: report,
    },
    null,
    2
  )
)

cdp.close()
chrome.kill()
await new Promise((r) =>
  chrome.exitCode !== null ? r() : chrome.once("exit", r)
)
for (const child of procs) child.kill()
await rm(work, { recursive: true, force: true, maxRetries: 5 })
await rm(profile, { recursive: true, force: true, maxRetries: 5 })
process.exit(0)
