/* DEV-ONLY login adapter — served exclusively when the operator starts the
 * service with DASHBOARD_DEV_LOGIN=1 (Config refuses it off loopback). It
 * implements the same dashboardAuth boundary as the production adapter:
 * getAccessToken resolves the current dev user's token; with no explicit
 * login it falls back to a DEFAULT DEV TOKEN (dev-alice) so local E2E never
 * blocks on authentication — the dev stand-in for the enterprise SSO session
 * the production adapter will inherit. An explicit login (in-page overlay,
 * no native dialogs) switches identity and stores "dev-<name>" in
 * localStorage, shared by every tab of this origin; a successful submit
 * reloads the page so the caller's flow (launcher/manage) continues alone.
 * Production deployments never set the flag and keep the fail-closed stub.
 */
window.dashboardAuth = Object.freeze({
  async getAccessToken() {
    // Default session: dev-alice (owner of the demo boards). Only an
    // explicit login/logout changes it; there is no unauthenticated state.
    return localStorage.getItem("dashboard-dev-token") || "dev-alice"
  },
  login() {
    if (document.getElementById("dashboard-dev-login")) return
    const overlay = document.createElement("div")
    overlay.id = "dashboard-dev-login"
    overlay.style.cssText =
      "position:fixed;inset:0;background:rgba(15,20,32,.55);display:flex;" +
      "align-items:center;justify-content:center;z-index:9999"
    const card = document.createElement("div")
    card.style.cssText =
      "background:#fff;color:#1f2933;border-radius:12px;padding:24px;" +
      "width:320px;font:14px/1.6 system-ui,sans-serif;box-shadow:0 12px 40px rgba(0,0,0,.25)"
    const title = document.createElement("div")
    title.textContent = "开发模式登录"
    title.style.cssText = "font-weight:600;margin-bottom:6px"
    const hint = document.createElement("div")
    hint.textContent =
      "输入 dev 用户名（如 alice）。仅本机演示，凭据为 dev-<name>。"
    hint.style.cssText = "color:#526073;font-size:12px;margin-bottom:14px"
    const input = document.createElement("input")
    input.placeholder = "用户名"
    input.autocomplete = "off"
    input.style.cssText =
      "width:100%;box-sizing:border-box;border:1px solid #c6d0e1;border-radius:8px;" +
      "padding:8px 12px;font-size:14px;margin-bottom:12px"
    const row = document.createElement("div")
    row.style.cssText = "display:flex;gap:8px;justify-content:flex-end"
    const cancel = document.createElement("button")
    cancel.type = "button"
    cancel.textContent = "取消"
    cancel.style.cssText =
      "border:1px solid #c6d0e1;background:#fff;border-radius:8px;padding:7px 16px;cursor:pointer"
    const confirm = document.createElement("button")
    confirm.type = "button"
    confirm.textContent = "登录"
    confirm.style.cssText =
      "border:0;background:#2563eb;color:#fff;border-radius:8px;padding:7px 20px;cursor:pointer"
    const error = document.createElement("div")
    error.style.cssText =
      "color:#a52b36;font-size:12px;margin-bottom:10px;min-height:16px"
    function submit() {
      const clean = input.value.trim().replace(/^dev-/, "")
      if (!/^[\w.-]{1,32}$/.test(clean)) {
        error.textContent = "用户名只能包含字母、数字、- _ ."
        return
      }
      localStorage.setItem("dashboard-dev-token", `dev-${clean}`)
      location.reload()
    }
    confirm.addEventListener("click", submit)
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") submit()
    })
    cancel.addEventListener("click", () => overlay.remove())
    row.append(cancel, confirm)
    card.append(title, hint, error, input, row)
    overlay.append(card)
    document.body.append(overlay)
    input.focus()
  },
  logout() {
    localStorage.removeItem("dashboard-dev-token")
    location.reload()
  },
})
