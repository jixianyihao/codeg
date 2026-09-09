;(() => {
  "use strict"
  // Control-origin launcher: mint a fresh short-lived capability with the
  // visitor's own identity, then navigate THIS tab to the content-origin
  // trusted loader. No iframe lives on the control origin (single layer).
  const $ = (id) => document.getElementById(id)
  const title = document.querySelector(".view-title")
  const pathMatch = location.pathname.match(
    /^\/dashboards\/([0-9a-f-]{36})(?:\/view)?\/?$/i,
  )
  const dashboardId = pathMatch?.[1]
  const status = $("view-status")
  const loginButton = $("view-login")
  const manageLink = $("view-manage")
  const retryButton = $("view-retry")

  function showAuthRequired(message) {
    status.setAttribute("role", "alert")
    title.textContent = message
    document.querySelectorAll("[data-auth]").forEach((node) => {
      node.hidden = !(window.dashboardAuth && typeof window.dashboardAuth.login === "function")
    })
    retryButton.hidden = true
    manageLink.hidden = true
  }

  function fail(message, showManage) {
    status.setAttribute("role", "alert")
    title.textContent = message
    document.querySelectorAll("[data-auth]").forEach((node) => {
      node.hidden = true
    })
    if (showManage && dashboardId) {
      manageLink.href = `/dashboards/${dashboardId}/manage`
      manageLink.textContent = "打开管理页"
      manageLink.hidden = false
    }
    retryButton.hidden = false
  }

  function errorText(error) {
    if (error?.code === "AUTH_REQUIRED") return "登录状态已失效，请重新登录。"
    if (error?.code === "AUTH_NOT_CONFIGURED") return "尚未接入企业 W3 登录。"
    if (error?.status === 401) return "登录状态已失效，请重新登录。"
    if (error?.status === 403 || error?.status === 404)
      return "看板不存在或当前身份无法访问。"
    return "内容暂时无法加载，请稍后重试。"
  }

  async function api(path, options = {}) {
    let token
    try {
      token = await window.dashboardAuth?.getAccessToken()
    } catch (error) {
      const failure = new Error("authentication")
      failure.code =
        error?.message === "AUTH_NOT_CONFIGURED"
          ? "AUTH_NOT_CONFIGURED"
          : "AUTH_REQUIRED"
      throw failure
    }
    const response = await fetch(`/api/v1${path}`, {
      ...options,
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
      signal: AbortSignal.timeout(30000),
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...options.headers,
        Authorization: `Bearer ${token}`,
      },
    })
    if (!response.ok) {
      const body = await response.json().catch(() => ({}))
      throw Object.assign(new Error("request-failed"), {
        status: response.status,
        code: body.code,
      })
    }
    return response.json()
  }

  function capabilityUrl(result, allowedOrigin) {
    // The service decides the destination; this page only verifies it lands
    // on the configured content origin at /view/{this dashboard}.
    try {
      const url = new URL(result.render_url, location.origin)
      if (
        url.origin !== allowedOrigin ||
        url.origin === location.origin ||
        url.pathname !== `/view/${dashboardId}` ||
        !url.hash ||
        url.search ||
        url.username ||
        url.password ||
        !["https:", "http:"].includes(url.protocol)
      ) {
        return null
      }
      return url
    } catch {
      return null
    }
  }

  async function launch() {
    if (!dashboardId) {
      fail("地址不正确。", false)
      return
    }
    status.removeAttribute("role")
    title.textContent = "正在安全加载看板…"
    document.querySelectorAll("[data-auth]").forEach((node) => {
      node.hidden = true
    })
    manageLink.hidden = true
    retryButton.hidden = true
    try {
      const params = new URLSearchParams(location.search)
      const versionId = params.get("version")
      if (versionId && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(versionId)) {
        fail("地址不正确。", false)
        return
      }
      const [supported, issued] = await Promise.all([
        api("/capabilities"),
        api(`/dashboards/${dashboardId}/view-capabilities`, {
          method: "POST",
          headers: { "Idempotency-Key": crypto.randomUUID() },
          body: JSON.stringify(versionId ? { version_id: versionId } : {}),
        }),
      ])
      const allowed = new URL(supported.content_origin)
      const url = capabilityUrl(issued, allowed.origin)
      if (!url) {
        fail("内容域配置不兼容，请联系管理员。", false)
        return
      }
      // Same-tab navigation to the content origin: the capability rides in
      // the fragment and is cleared by the loader itself.
      location.replace(url.href)
    } catch (error) {
      if (error?.code === "AUTH_REQUIRED" || error?.status === 401) {
        showAuthRequired(errorText(error))
        return
      }
      fail(errorText(error), true)
    }
  }

  loginButton?.addEventListener("click", () => {
    window.dashboardAuth?.login?.() // dev adapter; production W3 login
  })
  retryButton?.addEventListener("click", launch)
  launch()
})()
