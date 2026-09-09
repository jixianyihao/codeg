;(() => {
  "use strict"
  const $ = (id) => document.getElementById(id)
  const pathMatch = location.pathname.match(
    /^\/dashboards\/([0-9a-f-]{36})\/manage\/?$/i
  )
  const dashboardId = pathMatch?.[1]
  const base = `/dashboards/${encodeURIComponent(dashboardId || "")}`
  const roles = { owner: "所有者", editor: "编辑者", viewer: "查看者" }
  const types = {
    user: "企业用户",
    service: "服务账号",
    group: "用户群组",
    all_authenticated: "企业内公开",
  }
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone
  let dashboard,
    capabilities,
    me,
    selectedPrincipal,
    versionCursor,
    confirmAction
  let generation = 0,
    writing = false,
    pendingWrite = null
  let searchGeneration = 0
  const versions = new Map()

  function element(tag, text, className) {
    const node = document.createElement(tag)
    if (text !== undefined) node.textContent = text
    if (className) node.className = className
    return node
  }
  function button(text, action, className = "quiet") {
    const node = element("button", text, className)
    node.type = "button"
    node.addEventListener("click", action)
    return node
  }
  function formatDate(value) {
    if (!value) return "长期有效"
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return "未知时间"
    return new Intl.DateTimeFormat("zh-CN", {
      dateStyle: "medium",
      timeStyle: "short",
      timeZoneName: undefined,
    }).format(date)
  }
  function localInput(value) {
    if (!value) return ""
    const date = new Date(value)
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000)
      .toISOString()
      .slice(0, 16)
  }
  function utcInput(id) {
    const value = $(id).value
    if (!value) return null
    const date = new Date(value)
    if (Number.isNaN(date.getTime()))
      throw new Error("请输入有效的日期和时间。")
    return date.toISOString()
  }
  function dates(startId, endId) {
    const starts_at = utcInput(startId),
      expires_at = utcInput(endId)
    if (starts_at && expires_at && starts_at >= expires_at)
      throw new Error("到期时间必须晚于生效时间。")
    if (expires_at && new Date(expires_at).getTime() <= Date.now())
      throw new Error("到期时间必须晚于当前时间。")
    return { starts_at, expires_at }
  }
  function notice(message, error = false) {
    $("feedback").textContent = message
    $("feedback").classList.toggle("error", error)
    $("feedback").setAttribute("role", error ? "alert" : "status")
    $("feedback").hidden = false
  }
  function errorMessage(error) {
    const messages = {
      401: "登录状态已失效，请重新登录后再试。",
      403: "当前身份无权执行此操作。请刷新页面查看最新权限。",
      404: "看板不存在或当前身份无法访问。",
      409: "看板已被其他操作更新。请刷新页面后核对当前版本和权限，再重新操作。",
      413: "内容超出服务允许的大小。",
      429: "请求过于频繁，请稍后重试。",
      503: "服务或身份目录暂时不可用，请稍后重试。",
    }
    if (error.code === "AUTH_NOT_CONFIGURED")
      return "尚未接入企业 W3 登录。请联系管理员配置浏览器身份适配器。"
    if (error.code === "HUMAN_REQUIRED")
      return "此页面仅支持企业用户登录，请使用 W3 用户身份。"
    if (error.code === "INVALID_SERVICE")
      return "服务版本或内容域配置不兼容，请联系管理员。"
    if (error.status)
      return `${messages[error.status] || "操作未完成，请检查输入并稍后重试。"}${error.trace ? `\n追踪编号：${error.trace}` : ""}`
    return "网络请求未完成，请检查连接后重试。写操作可能已经完成，重试将使用原操作标识。"
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
      failure.status = failure.code === "AUTH_REQUIRED" ? 401 : undefined
      throw failure
    }
    if (typeof token !== "string" || !token || /[\r\n]/.test(token)) {
      throw Object.assign(new Error("authentication"), {
        code: "AUTH_NOT_CONFIGURED",
      })
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
    token = ""
    if (!response.ok) {
      const body = await response.json().catch(() => ({}))
      throw Object.assign(new Error("request-failed"), {
        status: response.status,
        trace: body.trace_id,
      })
    }
    return response.status === 204 ? null : response.json()
  }
  function canEdit() {
    return (
      ["owner", "editor"].includes(dashboard?.role) &&
      me?.scopes?.includes("write")
    )
  }
  function canManage() {
    return dashboard?.role === "owner" && me?.scopes?.includes("manage")
  }
  function configureRoles() {
    document.querySelectorAll("[data-editor]").forEach((node) => {
      node.hidden = !canEdit()
    })
    document.querySelectorAll("[data-owner]").forEach((node) => {
      node.hidden = !canManage()
    })
  }
  function showDashboard() {
    $("dashboard-title").textContent = dashboard.title
    document.title = `${dashboard.title} · AresClaw 看板`
    $("description").textContent = dashboard.description || "暂无摘要"
    $("owner").textContent =
      `${dashboard.owner_name} · ${dashboard.owner_type === "service" ? "服务账号" : "企业用户"}`
    $("role").textContent = roles[dashboard.role] || "未知权限"
    $("status").textContent =
      dashboard.status === "archived" ? "已下架" : "已发布"
    $("status").classList.toggle("archived", dashboard.status === "archived")
    $("published-at").textContent =
      `内容发布于 ${formatDate(dashboard.published_at)} (${timezone})`
    $("access-expiry").textContent = dashboard.expires_at
      ? `访问至 ${formatDate(dashboard.expires_at)} (${timezone})`
      : ""
    $("archive").textContent =
      dashboard.status === "archived" ? "恢复发布" : "下架看板"
    $("fullscreen-view").hidden = dashboard.status === "archived"
    configureRoles()
    $("loading").hidden = true
    $("dashboard").hidden = false
  }
  function showVersion(versionId) {
    // The manage page embeds no viewer: viewing opens the stable control
    // entry in a new tab, which re-authorizes and forwards to the
    // content-origin loader. A historical version rides in ?version=.
    if (dashboard.status === "archived") {
      notice("看板已下架。恢复发布后，仍在有效期内的授权对象可再次访问。", true)
      return
    }
    const suffix =
      versionId && versionId !== dashboard.current_version_id
        ? `?version=${encodeURIComponent(versionId)}`
        : ""
    window.open(`${base}${suffix}`, "_blank", "noopener")
  }
  function confirm(title, description, action) {
    $("confirm-heading").textContent = title
    $("confirm-description").textContent = description
    confirmAction = action
    $("confirm-dialog").showModal()
  }
  async function loadVersions(append = false) {
    const current = generation
    const result = await api(
      `${base}/versions${append && versionCursor ? `?cursor=${encodeURIComponent(versionCursor)}` : ""}`
    )
    if (current !== generation) return
    if (!append) {
      $("versions").replaceChildren()
      versions.clear()
    }
    for (const item of result.items) {
      if (versions.has(item.id)) continue
      versions.set(item.id, item)
      const row = element("li", undefined, "version-item")
      const heading = element("div", undefined, "version-top")
      heading.append(element("strong", `版本 ${item.number}`))
      if (item.id === dashboard.current_version_id)
        heading.append(element("span", "当前", "badge"))
      row.append(
        heading,
        element(
          "p",
          `${formatDate(item.created_at)}\n${(item.byte_size / 1024).toFixed(1)} KiB`,
          "version-details"
        )
      )
      const actions = element("div", undefined, "version-actions")
      if (dashboard.status !== "archived")
        actions.append(button("查看", () => showVersion(item.id)))
      if (
        canEdit() &&
        dashboard.status !== "archived" &&
        item.id !== dashboard.current_version_id
      ) {
        actions.append(
          button("恢复此版本", () =>
            confirm(
              "恢复历史版本",
              `将当前内容切换为版本 ${item.number}，已有授权和到期时间保持不变。`,
              () =>
                mutate(
                  `${base}/rollback`,
                  "POST",
                  { version_id: item.id },
                  "版本已恢复。"
                )
            )
          )
        )
      }
      row.append(actions)
      $("versions").append(row)
    }
    versionCursor = result.next_cursor
    $("more-versions").hidden = !versionCursor
    $("version-count").textContent =
      `${versions.size}${versionCursor ? "+" : ""} 个版本`
    if (!versions.size)
      $("versions").append(element("li", "暂无可见版本", "help"))
  }
  function editGrant(grant) {
    selectedPrincipal = {
      id: grant.subject_id,
      type: grant.subject_type,
      display_name: grant.subject_id,
    }
    $("principal-type").value = grant.subject_type
    $("principal-query").value = ""
    $("principal-results").replaceChildren()
    $("selected-principal").textContent =
      `已选择：${types[grant.subject_type]} · ${grant.subject_id}`
    $("grant-role").value = grant.role
    $("grant-start").value = localInput(grant.starts_at)
    $("grant-duration").value = grant.expires_at ? "custom" : "forever"
    $("grant-end").value = localInput(grant.expires_at)
    $("grant-end-label").hidden = !grant.expires_at
    $("grant-details").open = true
    $("grant-role").focus()
  }
  async function loadGrants() {
    if (!canManage()) return
    const current = generation
    const result = await api(`${base}/grants`)
    if (current !== generation) return
    // Use the revision associated with the currently rendered metadata. A later
    // ACL revision must cause a conflict instead of silently rebasing an edit.
    $("grants").replaceChildren()
    const publicRule = result.items.find(
      (item) => item.subject_type === "all_authenticated"
    )
    $("public-enabled").checked = Boolean(publicRule)
    $("public-start").value = localInput(publicRule?.starts_at)
    $("public-end").value = localInput(publicRule?.expires_at)
    for (const grant of result.items.filter(
      (item) => item.subject_type !== "all_authenticated"
    )) {
      const row = element("div", undefined, "grant-item")
      const expired =
        grant.expires_at && Date.parse(grant.expires_at) <= Date.now()
      row.append(
        element("strong", `${types[grant.subject_type]} · ${grant.subject_id}`)
      )
      row.append(
        element(
          "p",
          `${roles[grant.role]}${expired ? " · 已到期" : ""}\n${grant.starts_at ? `自 ${formatDate(grant.starts_at)} ` : ""}${grant.expires_at ? `至 ${formatDate(grant.expires_at)}` : "长期有效"} (${timezone})`
        )
      )
      const actions = element("div", undefined, "grant-actions")
      actions.append(button("修改 / 续期", () => editGrant(grant)))
      actions.append(
        button("撤销", () =>
          confirm(
            "撤销此条授权",
            `将移除 ${grant.subject_id} 的直接授权。其他用户、群组或公开授权仍可能提供访问权。`,
            () =>
              mutate(
                `${base}/grants/${encodeURIComponent(grant.subject_type)}/${encodeURIComponent(grant.subject_id)}`,
                "DELETE",
                null,
                "该条授权已撤销；其他有效授权仍然生效。"
              )
          )
        )
      )
      row.append(actions)
      $("grants").append(row)
    }
    if (!$("grants").children.length)
      $("grants").append(element("p", "暂无用户、服务账号或群组授权。", "help"))
  }
  async function showAccessSources() {
    // Current-permission diagnosis: only sources visible to the caller are
    // shown, and history sharing the same ACL is stated explicitly.
    const access = await api(`${base}/access`)
    const holder = $("access-sources")
    if (!holder) return
    holder.replaceChildren()
    if (!access.sources?.length) {
      holder.hidden = true
      return
    }
    holder.hidden = false
    for (const source of access.sources) {
      const label =
        source.subject_type === "owner"
          ? "看板所有者"
          : `${types[source.subject_type] || source.subject_type} · ${source.role}` +
            (source.expires_at ? ` · 至 ${formatDate(source.expires_at)}` : " · 长期")
      holder.append(element("li", label))
    }
    const note = element(
      "li",
      "历史版本与当前版本共用以上权限；撤销其中一条不一定完全失去访问。",
    )
    note.className = "help"
    holder.append(note)
  }

  async function reload() {
    const current = ++generation
    $("dashboard").hidden = true
    $("loading").hidden = false
    $("recovery").hidden = true
    try {
      if (!dashboardId)
        throw Object.assign(new Error("not-found"), { status: 404 })
      const [identity, supported] = await Promise.all([
        api("/me"),
        api("/capabilities"),
      ])
      if (current !== generation) return
      if (identity.principal_type !== "human")
        throw Object.assign(new Error("human-required"), {
          code: "HUMAN_REQUIRED",
        })
      if (supported.api_major !== 1 || !supported.content_origin)
        throw Object.assign(new Error("incompatible"), {
          code: "INVALID_SERVICE",
        })
      me = identity
      capabilities = supported
      $("identity").textContent = identity.display_name
      const nextDashboard = await api(base)
      if (current !== generation) return
      dashboard = nextDashboard
      showDashboard()
      showAccessSources().catch(() => {})
      const results = await Promise.allSettled([
        loadVersions(),
        loadGrants(),
        showVersion(dashboard.current_version_id),
      ])
      if (current !== generation) return
      const rejected = results.find((result) => result.status === "rejected")
      if (rejected) notice(errorMessage(rejected.reason), true)
    } catch (error) {
      if (current !== generation) return
      $("loading").hidden = true
      $("identity").textContent = "身份未就绪"
      $("recovery").hidden = false
      notice(errorMessage(error), true)
    }
  }
  async function mutate(path, method, payload, success, retry = false, dialogId = null) {
    if (writing) return
    if (pendingWrite && !retry) {
      notice(
        "上一次写操作的结果尚未确定。请先重试原操作，避免重复或覆盖修改。",
        true,
      )
      $("recovery").hidden = false
      $("retry-write").hidden = false
      return
    }
    const dialogError = (message) => {
      if (!dialogId) return
      const node = document.querySelector(`#${dialogId} [role="alert"]`)
      if (!node) return
      node.textContent = message
      node.hidden = false
    }
    const dialogErrorClear = () => {
      if (!dialogId) return
      const node = document.querySelector(`#${dialogId} [role="alert"]`)
      if (node) node.hidden = true
    }
    const request = retry
      ? pendingWrite
      : {
          path:
            method === "DELETE"
              ? `${path}?expected_revision=${dashboard.revision}`
              : path,
          method,
          body:
            method === "DELETE"
              ? undefined
              : JSON.stringify({
                  ...payload,
                  expected_revision: dashboard.revision,
                }),
          key: crypto.randomUUID(),
          success,
        }
    if (!request) return
    writing = true
    document.querySelectorAll("button").forEach((node) => {
      node.disabled = true
    })
    dialogErrorClear()
    try {
      await api(request.path, {
        method: request.method,
        body: request.body,
        headers: { "Idempotency-Key": request.key },
      })
      pendingWrite = null
      $("retry-write").hidden = true
      $("recovery").hidden = true
      dialogErrorClear()
      document
        .querySelectorAll("dialog[open]")
        .forEach((dialog) => dialog.close())
      await reload()
      if (!$("dashboard").hidden) notice(request.success)
    } catch (error) {
      const uncertain = !error.status || error.status >= 500
      pendingWrite = uncertain ? request : null
      $("recovery").hidden = false
      $("retry-write").hidden = !pendingWrite
      // In-dialog failures keep the dialog (and the user's draft) open with
      // the reason next to the fields; deterministic 4xx conflicts do not.
      dialogError(errorMessage(error))
      notice(errorMessage(error), true)
    } finally {
      writing = false
      document.querySelectorAll("button").forEach((node) => {
        node.disabled = false
      })
    }
  }
  async function searchPrincipals() {
    const query = $("principal-query").value.trim()
    if (!query) {
      notice("请先输入要搜索的名称。", true)
      return
    }
    const current = ++searchGeneration
    selectedPrincipal = null
    $("principal-results").textContent = "正在搜索…"
    try {
      const result = await api(
        `/principals?type=${encodeURIComponent($("principal-type").value)}&q=${encodeURIComponent(query)}`
      )
      if (current !== searchGeneration) return
      $("principal-results").replaceChildren()
      for (const item of result.items) {
        $("principal-results").append(
          button(`${item.display_name} · ${item.id}`, () => {
            selectedPrincipal = item
            $("selected-principal").textContent =
              `已选择：${item.display_name} · ${item.id}`
            $("principal-results").replaceChildren()
          })
        )
      }
      if (!result.items.length)
        $("principal-results").textContent =
          "未找到匹配对象。请使用更完整的名称搜索。"
      if (result.next_cursor)
        $("principal-results").append(
          element("p", "还有更多结果，请缩小搜索范围。", "help")
        )
    } catch (error) {
      if (current === searchGeneration) {
        $("principal-results").replaceChildren()
        notice(errorMessage(error), true)
      }
    }
  }
  $("reload").addEventListener("click", () => {
    const auth = window.dashboardAuth
    if (typeof auth?.login === "function") {
      // Dev adapter: always offer its in-page login (works in webviews
      // without native dialogs); production adapters stay fail-closed.
      auth.login()
      return
    }
    reload()
  })
  $("retry-write").addEventListener("click", () =>
    mutate("", "", null, "", true)
  )
  $("more-versions").addEventListener("click", () =>
    loadVersions(true).catch((error) => notice(errorMessage(error), true))
  )
  $("copy-link").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(
        `${location.origin}/dashboards/${dashboardId}`
      )
      notice("查看链接已复制。接收者仍需使用自己的身份登录。")
    } catch {
      notice("浏览器未允许复制。请从地址栏复制当前看板链接。", true)
    }
  })
  $("edit-metadata").addEventListener("click", () => {
    $("metadata-title").value = dashboard.title
    $("metadata-description").value = dashboard.description
    $("metadata-dialog").showModal()
  })
  document
    .querySelectorAll("[data-close]")
    .forEach((node) =>
      node.addEventListener("click", () => $(node.dataset.close).close())
    )
  $("confirm-action").addEventListener("click", () => {
    $("confirm-dialog").close()
    confirmAction?.()
  })
  $("metadata-form").addEventListener("submit", (event) => {
    event.preventDefault()
    const title = $("metadata-title").value.trim()
    if (!title) {
      notice("标题不能为空。", true)
      return
    }
    mutate(
      base,
      "PATCH",
      { title, description: $("metadata-description").value.trim() },
      "看板信息已更新，内容版本保持不变。",
      false,
      "metadata-dialog",
    )
  })
  $("archive").addEventListener("click", () => {
    const archived = dashboard.status === "archived"
    confirm(
      archived ? "恢复发布" : "下架看板",
      archived
        ? "恢复后，仍在有效期内的用户、群组及公开授权将重新生效。原有到期时间保持不变。"
        : "下架后将阻止新的内容访问。已下载或截图的内容无法收回，所有者可以稍后恢复发布。",
      () =>
        mutate(
          `${base}/${archived ? "restore" : "archive"}`,
          "POST",
          {},
          archived ? "看板已恢复发布。" : "看板已下架。"
        )
    )
  })
  $("fullscreen-view").addEventListener("click", () => {
    // Open the stable control entry in a new tab: it re-authorizes with the
    // visitor's identity and forwards to the content-origin loader.
    if (!dashboard || dashboard.status === "archived") return
    window.open(base, "_blank", "noopener")
  })

  $("search-principals").addEventListener("click", searchPrincipals)
  $("principal-query").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault()
      searchPrincipals()
    }
  })
  for (const id of ["principal-query", "principal-type"])
    $(id).addEventListener("input", () => {
      ++searchGeneration
      selectedPrincipal = null
      $("selected-principal").textContent = "请从真实目录结果中选择对象。"
      $("principal-results").replaceChildren()
    })
  $("grant-duration").addEventListener("change", () => {
    $("grant-end-label").hidden = $("grant-duration").value !== "custom"
  })
  $("grant-form").addEventListener("submit", (event) => {
    event.preventDefault()
    if (!selectedPrincipal) {
      notice("请先从目录搜索结果中选择授权对象。", true)
      return
    }
    try {
      const duration = $("grant-duration").value
      let expiry = { starts_at: utcInput("grant-start"), expires_at: null }
      if (duration === "custom") expiry = dates("grant-start", "grant-end")
      if (["1", "7"].includes(duration)) {
        const start = Math.max(
          Date.now(),
          expiry.starts_at ? Date.parse(expiry.starts_at) : 0
        )
        expiry.expires_at = new Date(
          start + Number(duration) * 86400000
        ).toISOString()
      }
      mutate(
        `${base}/grants`,
        "POST",
        {
          subject_type: selectedPrincipal.type,
          subject_id: selectedPrincipal.id,
          role: $("grant-role").value,
          ...expiry,
        },
        `授权已保存。${expiry.expires_at ? `到期时间：${formatDate(expiry.expires_at)} (${timezone})。` : "长期有效。"}`
      )
    } catch (error) {
      notice(error.message, true)
    }
  })
  $("public-form").addEventListener("submit", (event) => {
    event.preventDefault()
    try {
      const enabled = $("public-enabled").checked
      const expiry = enabled ? dates("public-start", "public-end") : {}
      mutate(
        `${base}/public-access`,
        "PUT",
        { enabled, ...expiry },
        `公开设置已更新。${enabled ? `当前及历史版本适用。${expiry.expires_at ? `到期时间：${formatDate(expiry.expires_at)} (${timezone})。` : "长期有效。"}` : "已关闭企业内公开，其他有效授权保持不变。"}`
      )
    } catch (error) {
      notice(error.message, true)
    }
  })
  document.querySelectorAll(".timezone").forEach((node) => {
    node.textContent = `时间按 ${timezone} 展示，保存时转换为 UTC。`
  })
  reload()
})()
