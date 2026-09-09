;(async () => {
  "use strict"
  // Trusted loader: the capability is read from the fragment and cleared
  // immediately; the only credential kept lives in this closure's memory.
  let capability = location.hash.slice(1)
  history.replaceState(null, "", location.pathname)
  const status = document.getElementById("render-status")
  const controlOrigin = document.querySelector(
    'meta[name="x-dashboard-control-origin"]'
  )?.content
  const dashboardId = document.querySelector(
    'meta[name="x-dashboard-id"]'
  )?.content
  const linkFallback = document.getElementById("render-link-fallback")
  const linkOpen = document.getElementById("render-link-open")
  const linkHint = document.getElementById("render-link-hint")
  document.getElementById("render-link-close").addEventListener("click", () => {
    linkFallback.hidden = true
  })

  function externalUrl(value) {
    if (typeof value !== "string" || !value || value.length > 4096) return null
    try {
      const url = new URL(value)
      if (
        !["http:", "https:"].includes(url.protocol) ||
        url.username ||
        url.password ||
        url.href.length > 4096
      ) {
        return null
      }
      return url.href
    } catch {
      return null
    }
  }

  // Serialized into the prefix before any generated script. Keep the private
  // port and captured DOM methods in this closure, never on window or the DOM.
  function installLinkHandler(parentOrigin) {
    const channel = new MessageChannel()
    const send = channel.port1.postMessage.bind(channel.port1)
    const call = Function.prototype.call.bind(Function.prototype.call)
    const pathOf = call.bind(null, Event.prototype.composedPath)
    const attributeOf = call.bind(null, Element.prototype.getAttribute)
    const hrefOf = call.bind(
      null,
      Object.getOwnPropertyDescriptor(HTMLAnchorElement.prototype, "href").get
    )
    const buttonOf = call.bind(
      null,
      Object.getOwnPropertyDescriptor(MouseEvent.prototype, "button").get
    )
    const trim = call.bind(null, String.prototype.trim)
    const prevent = call.bind(null, Event.prototype.preventDefault)
    const stop = call.bind(null, Event.prototype.stopImmediatePropagation)
    const wasPrevented = call.bind(
      null,
      Object.getOwnPropertyDescriptor(Event.prototype, "defaultPrevented").get
    )
    const isHttp = RegExp.prototype.test.bind(/^https?:\/\//i)

    function anchorOf(event) {
      const path = pathOf(event)
      for (let index = 0; index < path.length; index++) {
        let raw, href
        try {
          // Native href getter also verifies this is an actual HTML anchor.
          raw = attributeOf(path[index], "href")
          href = hrefOf(path[index])
        } catch {
          continue
        }
        if (raw === null || !(raw = trim(raw))) return null
        return { raw, href }
      }
      return null
    }
    function follow(event, button) {
      if (!event.isTrusted || buttonOf(event) !== button) return
      const anchor = anchorOf(event)
      if (!anchor || anchor.raw[0] === "#") return
      prevent(event)
      stop(event)
      if (isHttp(anchor.href)) send({ url: anchor.href })
    }
    window.addEventListener("click", (event) => follow(event, 0), true)
    window.addEventListener("auxclick", (event) => follow(event, 1), true)
    window.addEventListener("click", (event) => {
      if (!event.isTrusted || buttonOf(event) !== 0 || wasPrevented(event))
        return
      const anchor = anchorOf(event)
      if (!anchor || anchor.raw[0] !== "#") return
      // srcdoc inherits the loader's base URL. After report document handlers,
      // navigate this frame's fragment rather than that parent-origin URL.
      prevent(event)
      location.hash = anchor.raw
    })
    parent.postMessage(
      { type: "aresclaw-dashboard-link-ready" },
      parentOrigin,
      [channel.port2]
    )
  }

  function fail() {
    capability = ""
    status.setAttribute("role", "alert")
    status.replaceChildren(
      "内容无法加载，访问权限可能已失效或已过期。",
      document.createElement("br")
    )
    if (controlOrigin && dashboardId) {
      const back = document.createElement("a")
      back.href = `${controlOrigin}/dashboards/${dashboardId}`
      back.textContent = "返回看板入口重新授权"
      back.rel = "noopener"
      status.append(back)
    } else {
      // No positional information: never guess a dashboard or permission.
      status.append("请从看板链接重新进入。")
    }
  }
  try {
    if (
      !capability ||
      capability.length > 4096 ||
      /[\s\r\n]/.test(capability)
    ) {
      throw new Error("invalid-capability")
    }
    const response = await fetch("/content", {
      headers: { Authorization: `Bearer ${capability}` },
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
      signal: AbortSignal.timeout(30000),
    })
    capability = ""
    if (
      !response.ok ||
      !/^text\/plain(?:;|$)/i.test(response.headers.get("content-type") || "")
    ) {
      throw new Error("content-unavailable")
    }
    const html = await response.text()
    const frame = document.createElement("iframe")
    frame.id = "dashboard-content"
    frame.title = "已发布的看板内容"
    frame.setAttribute("sandbox", "allow-scripts")
    frame.setAttribute("referrerpolicy", "no-referrer")
    // This policy precedes all generated bytes. Further CSP policies in the
    // document can only restrict it. The iframe enforces the opaque origin.
    const policy =
      "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src 'none'; connect-src 'none'; frame-src 'none'; object-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'"
    frame.srcdoc = `<!doctype html><meta http-equiv="Content-Security-Policy" content="${policy}"><meta name="referrer" content="no-referrer"><script>(${installLinkHandler.toString()})(${JSON.stringify(location.origin)})</script>${html}`
    function receiveLinkChannel(event) {
      if (
        event.source !== frame.contentWindow ||
        event.origin !== "null" ||
        !event.data ||
        typeof event.data !== "object" ||
        Array.isArray(event.data) ||
        Object.keys(event.data).length !== 1 ||
        event.data.type !== "aresclaw-dashboard-link-ready" ||
        event.ports.length !== 1
      ) {
        return
      }
      // The prefix queues this before generated scripts can send their own
      // READY. Never let a later message or frame navigation replace the port.
      window.removeEventListener("message", receiveLinkChannel)
      const port = event.ports[0]
      port.onmessage = ({ data }) => {
        if (
          !data ||
          typeof data !== "object" ||
          Array.isArray(data) ||
          Object.keys(data).length !== 1
        ) {
          return
        }
        const url = externalUrl(data.url)
        if (!url) return
        const activation = navigator.userActivation
        const canOpen = activation?.isActive === true
        linkOpen.href = url
        linkOpen.textContent = url
        linkHint.textContent = canOpen
          ? "若新标签页未打开，请点击："
          : "打开外部链接："
        linkFallback.hidden = false
        if (canOpen) {
          // noopener may return null after successfully opening the tab. The
          // neutral manual link remains available for browser popup policies.
          try {
            window.open(url, "_blank", "noopener,noreferrer")
          } catch {
            // The manual link above provides a fresh user gesture.
          }
        }
      }
    }
    window.addEventListener("message", receiveLinkChannel)
    document.body.append(frame)
    status.hidden = true
  } catch {
    fail()
  }
})()
