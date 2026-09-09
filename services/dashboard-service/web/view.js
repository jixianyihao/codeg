;(() => {
  "use strict"
  const $ = (id) => document.getElementById(id)
  const dashboardId = location.pathname.match(
    /^\/dashboards\/([0-9a-f-]{36})\/view\/?$/i,
  )?.[1]
  const status = $("view-status")
  const frame = $("view-frame")

  function fail(message, showLogin) {
    frame.hidden = true
    status.hidden = false
    status.setAttribute("role", "alert")
    status.replaceChildren()
    status.append(message)
    if (showLogin && typeof window.dashboardAuth?.login === "function") {
      const login = document.createElement("button")
      login.id = "view-login"
      login.type = "button"
      login.textContent = "登录后查看"
      login.addEventListener("click", () => {
        window.dashboardAuth.login() // dev adapter; production W3 login
      })
      status.append(login)
    }
  }

  function errorText(error) {
    if (error?.code === "AUTH_REQUIRED" || error?.code === "AUTH_NOT_CONFIGURED")
      return "尚未登录。"
    if (error?.status === 401) return "登录状态已失效，请重新登录。"
    if (error?.status === 404) return "看板不存在或当前身份无法访问。"
    return "内容暂时无法加载，请稍后重试。"
  }

  async function api(path, options = {}) {
    let token
    try {
      token = await window.dashboardAuth?.getAccessToken()
    } catch (error) {
      const failure = new Error("authentication")
      failure.code = error?.message === "AUTH_NOT_CONFIGURED" ? "AUTH_NOT_CONFIGURED" : "AUTH_REQUIRED"
      throw failure
    }
    const response = await fetch(`/api/v1${path}`, {
      ...options,
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
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

  async function load() {
    if (!dashboardId) {
      fail("地址不正确。")
      return
    }
    status.hidden = false
    frame.hidden = true
    try {
      const [supported] = await Promise.all([api("/capabilities")])
      const result = await api(`/dashboards/${dashboardId}/view-capabilities`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({}),
      })
      const url = new URL(result.render_url, location.origin)
      const allowed = new URL(supported.content_origin)
      if (
        url.origin !== allowed.origin ||
        url.origin === location.origin ||
        url.pathname !== "/render" ||
        !url.hash ||
        url.search ||
        url.username ||
        url.password
      ) {
        fail("内容域配置不兼容，请联系管理员。")
        return
      }
      frame.src = url.href
      frame.hidden = false
      status.hidden = true
    } catch (error) {
      fail(errorText(error), true)
    }
  }

  load()
})()
