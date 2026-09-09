import assert from "node:assert/strict"
import { spawn } from "node:child_process"
import { existsSync } from "node:fs"
import { mkdtemp, readFile, rm } from "node:fs/promises"
import http from "node:http"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { after, before, test } from "node:test"

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const browserPath =
  process.env.DASHBOARD_TEST_BROWSER ||
  [
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
  ].find(existsSync)
const id = "11111111-1111-4111-8111-111111111111"
const version = "22222222-2222-4222-8222-222222222222"
let chrome, cdp, profile, control, content, origin, contentOrigin
let role = "viewer",
  denied = false,
  recorded = [],
  publicGrant = null
let identityType = "human",
  authConfigured = true,
  renderOverride = null,
  failPublicOnce = false
let patchConflict = false
let html = ""
const browserErrors = []
let sequence = 0
const waiting = new Map()
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

// Real response policies mirrored from dashboard_service.app/content_app so
// the browser exercises the actual CSP the service ships (R12 verification).
const CONTROL_CSP =
  "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; " +
  "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
const BOOTSTRAP_CSP =
  "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; " +
  "img-src data: blob:; font-src data:; connect-src 'self'; frame-src about:; " +
  "object-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'; " +
  "frame-ancestors 'none'"

function call(method, params = {}, sessionId) {
  const commandId = ++sequence
  return new Promise((resolve, reject) => {
    waiting.set(commandId, { resolve, reject })
    cdp.send(
      JSON.stringify({
        id: commandId,
        method,
        params,
        ...(sessionId ? { sessionId } : {}),
      })
    )
  })
}

async function evaluate(expression, contextId) {
  const response = await call("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
    ...(contextId ? { contextId } : {}),
  })
  if (response.exceptionDetails) throw new Error(response.exceptionDetails.text)
  return response.result.value
}

async function until(expression, contextId, timeout = 5000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    if (await evaluate(expression, contextId)) return
    await delay(40)
  }
  assert.fail(
    `Browser condition did not become true: ${expression}\n${browserErrors.slice(-5).join("\n")}`
  )
}

async function json(res, value, status = 200) {
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
  })
  res.end(JSON.stringify(value))
}

async function asset(res, name, extraHeaders = {}) {
  try {
    const bytes = await readFile(path.join(webRoot, name))
    const mime = name.endsWith(".html")
      ? "text/html"
      : name.endsWith(".css")
        ? "text/css"
        : "text/javascript"
    res.writeHead(200, {
      "Content-Type": `${mime}; charset=utf-8`,
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      ...extraHeaders,
    })
    res.end(bytes)
  } catch {
    res.writeHead(404)
    res.end("Missing page asset")
  }
}

async function loaderPage(res, dashboardId) {
  // Mirrors content_app._loader_response: inject the trusted coordinates of
  // the back-to-control link, keep the capability out of the served bytes.
  const raw = await readFile(path.join(webRoot, "render.html"), "utf8")
  const marker =
    `<meta name="x-dashboard-control-origin" content="${origin}">` +
    `<meta name="x-dashboard-id" content="${dashboardId}">`
  const page = raw.replace("</head>", marker + "</head>")
  res.writeHead(200, {
    "Content-Type": "text/html; charset=utf-8",
    "Content-Security-Policy": BOOTSTRAP_CSP,
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
  })
  res.end(page)
}

async function start(handler) {
  const server = http.createServer((req, res) => {
    Promise.resolve(handler(req, res)).catch(() => {
      res.statusCode = 500
      res.end()
    })
  })
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve))
  return server
}

before(async () => {
  assert.ok(browserPath, "Set DASHBOARD_TEST_BROWSER to a Chromium executable")
  content = await start(async (req, res) => {
    const url = new URL(req.url, "http://localhost")
    recorded.push({
      origin: "content",
      path: url.pathname,
      authorization: req.headers.authorization,
    })
    const viewMatch = url.pathname.match(/^\/view\/([0-9a-f-]{36})\/?$/i)
    if (viewMatch) return loaderPage(res, viewMatch[1])
    if (url.pathname === "/render")
      return asset(res, "render.html", {
        "Content-Security-Policy": BOOTSTRAP_CSP,
      })
    if (url.pathname === "/render.js") return asset(res, "render.js")
    if (url.pathname === "/content") {
      if (denied || req.headers.authorization !== "Bearer test-capability")
        return json(res, { code: "FORBIDDEN" }, 403)
      res.writeHead(200, {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "no-store",
      })
      return res.end(html)
    }
    res.writeHead(404)
    res.end()
  })
  contentOrigin = `http://127.0.0.1:${content.address().port}`
  control = await start(async (req, res) => {
    const url = new URL(req.url, "http://localhost")
    if (url.pathname === "/auth-provider.js") {
      if (!authConfigured) return asset(res, "auth-provider.js")
      res.writeHead(200, { "Content-Type": "text/javascript" })
      return res.end(
        'window.dashboardAuth = { getAccessToken: async () => "human-test-token" }'
      )
    }
    // Real route split: /dashboards/{id}[/view] is the launcher, /manage is
    // the admin page; both carry the shipped control CSP.
    if (/^\/dashboards\/[0-9a-f-]{36}\/manage\/?$/i.test(url.pathname))
      return asset(res, "index.html", {
        "Content-Security-Policy": CONTROL_CSP,
      })
    if (/^\/dashboards\/[0-9a-f-]{36}(\/view)?\/?$/i.test(url.pathname))
      return asset(res, "view.html", { "Content-Security-Policy": CONTROL_CSP })
    if (
      ["/app.js", "/styles.css", "/view.js", "/view.css"].includes(url.pathname)
    )
      return asset(res, url.pathname.slice(1))
    let body = ""
    for await (const chunk of req) body += chunk
    recorded.push({
      origin: "control",
      path: url.pathname,
      method: req.method,
      body: body ? JSON.parse(body) : null,
      key: req.headers["idempotency-key"],
    })
    if (req.headers.authorization !== "Bearer human-test-token")
      return json(res, { code: "UNAUTHENTICATED" }, 401)
    if (url.pathname === "/api/v1/me")
      return json(res, {
        principal_id: "human-1",
        principal_type: identityType,
        display_name: "测试用户",
        scopes: ["read", "write", "manage"],
        is_admin: false,
      })
    if (url.pathname === "/api/v1/capabilities")
      return json(res, {
        api_major: 1,
        features: [],
        max_upload_bytes: 10485760,
        auth_methods: ["w3"],
        content_origin: contentOrigin,
      })
    if (
      url.pathname === `/api/v1/dashboards/${id}` &&
      req.method === "PATCH" &&
      patchConflict
    )
      return json(res, { code: "REVISION_CONFLICT" }, 409)
    if (url.pathname === `/api/v1/dashboards/${id}`)
      return json(res, {
        id,
        title: '<img src=x onerror="window.trustedCompromised=true">',
        description: "静态数据快照",
        owner_name: "示例团队",
        owner_type: "human",
        owner_principal_id: "human-1",
        current_version_id: version,
        revision: 7,
        status: "published",
        role,
        created_at: "2026-09-01T00:00:00Z",
        updated_at: "2026-09-09T00:00:00Z",
        published_at: "2026-09-08T00:00:00Z",
        expires_at: null,
        view_url: `${origin}/dashboards/${id}`,
      })
    if (url.pathname.endsWith("/versions"))
      return json(res, {
        items: [
          {
            id: version,
            number: 1,
            created_at: "2026-09-08T00:00:00Z",
            byte_size: 200,
            created_by: "human-1",
          },
        ],
        next_cursor: null,
      })
    if (url.pathname.endsWith("/view-capabilities"))
      return json(res, {
        render_url:
          renderOverride || `${contentOrigin}/view/${id}#test-capability`,
        expires_at: new Date(Date.now() + 60000).toISOString(),
      })
    if (url.pathname.endsWith("/grants"))
      return json(res, { items: publicGrant ? [publicGrant] : [], revision: 7 })
    if (url.pathname === "/api/v1/principals")
      return json(res, {
        items: [{ id: "human-2", type: "user", display_name: "张三 <script>" }],
        next_cursor: null,
      })
    if (url.pathname.endsWith("/public-access")) {
      if (failPublicOnce) {
        failPublicOnce = false
        return json(res, { code: "TEMPORARILY_UNAVAILABLE" }, 503)
      }
      publicGrant = {
        subject_type: "all_authenticated",
        subject_id: "*",
        role: "viewer",
        starts_at: null,
        expires_at: null,
      }
      return json(res, { revision: 8 })
    }
    return json(res, { code: "NOT_FOUND" }, 404)
  })
  origin = `http://127.0.0.1:${control.address().port}`
  profile = await mkdtemp(path.join(webRoot, "tests", ".browser-"))
  chrome = spawn(
    browserPath,
    [
      "--headless=new",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-gpu",
      "--disable-background-networking",
      "--remote-debugging-port=0",
      `--user-data-dir=${profile}`,
      "about:blank",
    ],
    { windowsHide: true, stdio: "ignore" }
  )
  let port
  for (let attempt = 0; attempt < 100; attempt++) {
    try {
      port = (
        await readFile(path.join(profile, "DevToolsActivePort"), "utf8")
      ).split("\n")[0]
      break
    } catch {
      await delay(100)
    }
  }
  assert.ok(port, "Chromium did not start")
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
  cdp = new WebSocket(
    pages.find((page) => page.type === "page").webSocketDebuggerUrl
  )
  cdp.addEventListener("message", ({ data }) => {
    const message = JSON.parse(data)
    if (message.method === "Target.attachedToTarget")
      call("Log.enable", {}, message.params.sessionId).catch(() => {})
    if (
      message.method === "Log.entryAdded" &&
      message.params.entry.level === "error"
    )
      browserErrors.push(message.params.entry.text)
    const pending = waiting.get(message.id)
    if (!pending) return
    waiting.delete(message.id)
    if (message.error) pending.reject(new Error(message.error.message))
    else pending.resolve(message.result)
  })
  await new Promise((resolve) =>
    cdp.addEventListener("open", resolve, { once: true })
  )
  await call("Page.enable")
  await call("Runtime.enable")
  await call("Log.enable")
  await call("Target.setAutoAttach", {
    autoAttach: true,
    waitForDebuggerOnStart: false,
    flatten: true,
  })
  await call("Page.addScriptToEvaluateOnNewDocument", {
    source:
      'window.testMessages = []; window.addEventListener("message", event => window.testMessages.push(event.data))',
  })
})

after(async () => {
  cdp?.close()
  if (chrome) {
    chrome.kill()
    await new Promise((resolve) => {
      if (chrome.exitCode !== null) resolve()
      else chrome.once("exit", resolve)
    })
  }
  await Promise.all(
    [control, content]
      .filter(Boolean)
      .map((server) => new Promise((resolve) => server.close(resolve)))
  )
  if (profile && profile.startsWith(path.join(webRoot, "tests", ".browser-"))) {
    await rm(profile, {
      recursive: true,
      force: true,
      maxRetries: 10,
      retryDelay: 100,
    })
  }
})

test("launcher forwards the same tab to the content loader: single layer, no control iframe", async () => {
  role = "viewer"
  denied = false
  recorded = []
  html = '<!doctype html><h1 id="sample">已加载</h1>'
  await call("Page.navigate", { url: `${origin}/dashboards/${id}` })
  // The launcher replaces this very tab onto the content origin.
  await until(
    `location.origin === "${contentOrigin}" && location.pathname === "/view/${id}"`
  )
  await until(
    'document.querySelector("#dashboard-content")?.srcdoc.includes("已加载")'
  )
  assert.equal(await evaluate("location.hash"), "")
  // Exactly one capability was minted and redeemed; nothing loaded the
  // control origin's pages into a frame (there is no frame on either side
  // except the single sandboxed content iframe).
  const minted = recorded.filter((request) =>
    request.path.endsWith("/view-capabilities")
  )
  assert.equal(minted.length, 1)
  const fetched = recorded.filter((request) => request.path === "/content")
  assert.equal(fetched.length, 1)
  assert.equal(fetched[0].authorization, "Bearer test-capability")
  assert.equal(await evaluate("document.querySelectorAll('iframe').length"), 1)
})

test("viewer receives literal metadata and cannot use edit, sharing, or archive controls", async () => {
  role = "viewer"
  denied = false
  recorded = []
  html = '<!doctype html><h1 id="sample">已加载</h1>'
  await call("Page.navigate", { url: `${origin}/dashboards/${id}/manage` })
  await until(
    'document.querySelector("#dashboard-title")?.textContent.includes("<img")'
  )
  assert.equal(await evaluate("Boolean(window.trustedCompromised)"), false)
  assert.equal(
    await evaluate('document.querySelector("#dashboard-title img") !== null'),
    false
  )
  assert.equal(
    await evaluate(
      '[...document.querySelectorAll("[data-editor], [data-owner]")].some(el => !el.hidden && el.getClientRects().length)'
    ),
    false
  )
  assert.equal(
    recorded.some((request) => request.path.endsWith("/grants")),
    false
  )
  // The manage page embeds no viewer under the shipped control CSP.
  assert.equal(await evaluate("document.querySelectorAll('iframe').length"), 0)
})

test("metadata conflict is visible inside the editing dialog and preserves the draft", async () => {
  role = "owner"
  patchConflict = true
  await call("Page.navigate", { url: `${origin}/dashboards/${id}/manage` })
  await until(
    'document.querySelector("#edit-metadata") && !document.querySelector("#edit-metadata").hidden'
  )
  await evaluate(
    'document.querySelector("#edit-metadata").click(); document.querySelector("#metadata-title").value="修订标题"; document.querySelector("#metadata-form").requestSubmit()'
  )
  await until(
    'document.querySelector("#metadata-dialog[open] [role=alert]")?.textContent.includes("其他操作更新")'
  )
  assert.equal(
    await evaluate('document.querySelector("#metadata-title").value'),
    "修订标题"
  )
  patchConflict = false
})

test("sandbox renders interactive HTML but blocks parent access, external calls, and top navigation", async () => {
  html = `<!doctype html><button id="increment" style="width:160px;height:80px" onclick="this.textContent='完成';parent.postMessage('interaction-complete','*')">运行</button>
    <img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7" onload="parent.postMessage('embedded-image-loaded','*')">
    <img src="${origin}/attack"><style>body{background-image:url('${origin}/attack-css')}</style>
    <script>try { top.trustedCompromised = true } catch {}
    try { parent.localStorage.setItem('attack', 'yes') } catch {}
    try { top.location.href = '${origin}/attack' } catch {}
    fetch('${origin}/api/v1/attack').catch(() => {});<\/script>`
  recorded = []
  await call("Page.navigate", {
    url: `${contentOrigin}/view/${id}#test-capability`,
  })
  await until(
    'location.hash === "" && document.querySelector("#dashboard-content") !== null'
  )
  await until(
    'document.querySelector("#dashboard-content")?.srcdoc.includes("increment")'
  )
  // R12: under the real bootstrap CSP the single iframe fills the viewport
  // (no default 300x150, no double scrollbars).
  await until(
    '(() => { const r = document.querySelector("#dashboard-content").getBoundingClientRect();' +
      "return r.width >= window.innerWidth - 2 && r.height >= window.innerHeight - 2 })()"
  )
  await delay(250)
  // Click until the sandboxed inline handler reports back; retrying guards
  // against a click landing before the iframe finished layout on slow hosts.
  for (let attempt = 0; attempt < 20; attempt++) {
    if (await evaluate('window.testMessages.includes("interaction-complete")'))
      break
    await call("Input.dispatchMouseEvent", {
      type: "mousePressed",
      x: 50,
      y: 40,
      button: "left",
      clickCount: 1,
    })
    await call("Input.dispatchMouseEvent", {
      type: "mouseReleased",
      x: 50,
      y: 40,
      button: "left",
      clickCount: 1,
    })
    await delay(250)
  }
  await until('window.testMessages.includes("interaction-complete")')
  assert.equal(
    await evaluate('window.testMessages.includes("embedded-image-loaded")'),
    true
  )
  assert.equal(await evaluate("Boolean(window.trustedCompromised)"), false)
  assert.equal(await evaluate('localStorage.getItem("attack")'), null)
  assert.equal(await evaluate("location.pathname"), `/view/${id}`)
  assert.equal(
    recorded.some((request) => request.path.includes("attack")),
    false
  )
  assert.equal(
    recorded.filter((request) => request.path === "/content")[0].authorization,
    "Bearer test-capability"
  )
})

test("revoked capability shows the back-to-control entry, never a frame", async () => {
  denied = true
  await call("Page.navigate", { url: "about:blank" })
  await until('location.href === "about:blank"')
  await call("Page.navigate", {
    url: `${contentOrigin}/view/${id}#test-capability`,
  })
  await until(
    'document.querySelector("#render-status")?.getAttribute("role") === "alert"'
  )
  assert.equal(
    await evaluate('document.querySelector("#dashboard-content")'),
    null
  )
  assert.equal(await evaluate("location.hash"), "")
  // The trusted coordinates injected by the server build the way back; the
  // page never guesses a dashboard on its own.
  assert.equal(
    await evaluate('document.querySelector("#render-status a")?.href'),
    `${origin}/dashboards/${id}`
  )
  denied = false
})

test("owner can publish a public permission change with revision and a fresh idempotency key", async () => {
  role = "owner"
  recorded = []
  publicGrant = null
  await call("Page.navigate", { url: `${origin}/dashboards/${id}/manage` })
  await until(
    'document.querySelector("#public-enabled") && !document.querySelector("#sharing-panel").hidden'
  )
  await evaluate(
    'document.querySelector("#public-enabled").click(); document.querySelector("#public-form").requestSubmit()'
  )
  await until(
    'document.querySelector("#feedback")?.textContent.includes("公开设置已更新")'
  )
  const request = recorded.find((entry) =>
    entry.path.endsWith("/public-access")
  )
  assert.equal(request.method, "PUT")
  assert.equal(request.body.enabled, true)
  assert.equal(request.body.expected_revision, 7)
  assert.match(request.key, /^[0-9a-f-]{36}$/)
})

test("missing W3 deployment adapter fails closed without requesting protected data", async () => {
  authConfigured = false
  recorded = []
  await call("Page.navigate", { url: `${origin}/dashboards/${id}/manage` })
  await until(
    'document.querySelector("#feedback")?.textContent.includes("尚未接入")'
  )
  assert.equal(
    recorded.some((request) => request.path.startsWith("/api/v1/")),
    false
  )
  assert.equal(
    await evaluate('document.querySelector("#dashboard").hidden'),
    true
  )
  authConfigured = true
})

test("machine principal cannot use the human browser login seam", async () => {
  identityType = "service"
  recorded = []
  await call("Page.navigate", { url: `${origin}/dashboards/${id}/manage` })
  await until(
    'document.querySelector("#feedback")?.textContent.includes("仅支持企业用户")'
  )
  assert.equal(
    recorded.some((request) => request.path === `/api/v1/dashboards/${id}`),
    false
  )
  identityType = "human"
})

test("unconfigured rendering origin is never loaded by the trusted shell", async () => {
  renderOverride = `${origin}/attack#test-capability`
  recorded = []
  await call("Page.navigate", { url: `${origin}/dashboards/${id}` })
  await until(
    'document.querySelector("#view-status")?.textContent.includes("内容域配置不兼容")'
  )
  assert.equal(await evaluate("document.querySelectorAll('iframe').length"), 0)
  assert.equal(
    recorded.some((request) => request.path === "/attack"),
    false
  )
  // The tab stayed on the control origin: no navigation happened.
  assert.equal(await evaluate("location.origin"), origin)
  renderOverride = null
})

test("retry after uncertain permission mutation reuses its key, revision and absolute expiry", async () => {
  role = "owner"
  publicGrant = null
  failPublicOnce = true
  recorded = []
  await call("Page.navigate", { url: `${origin}/dashboards/${id}/manage` })
  await until(
    'document.querySelector("#sharing-panel") && !document.querySelector("#sharing-panel").hidden && document.querySelector("#grants").textContent.includes("暂无")'
  )
  await evaluate(
    'document.querySelector("#public-enabled").click(); document.querySelector("#public-end").value="2030-10-01T18:00"; document.querySelector("#public-form").requestSubmit()'
  )
  await until(
    'document.querySelector("#retry-write") && !document.querySelector("#retry-write").hidden && !document.querySelector("#retry-write").disabled'
  )
  assert.equal(
    await evaluate(
      'document.querySelector("#feedback").textContent.includes("已更新")'
    ),
    false
  )
  await evaluate('document.querySelector("#retry-write").click()')
  await until(
    'document.querySelector("#feedback")?.textContent.includes("公开设置已更新")'
  )
  const writes = recorded.filter((entry) =>
    entry.path.endsWith("/public-access")
  )
  assert.equal(writes.length, 2)
  assert.equal(writes[0].key, writes[1].key)
  assert.deepEqual(writes[0].body, writes[1].body)
  assert.match(writes[1].body.expires_at, /^2030-10-01T\d\d:00:00.000Z$/)
})
