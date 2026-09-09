/* DEV-ONLY login adapter — served exclusively when the operator starts the
 * service with DASHBOARD_DEV_LOGIN=1 (Config refuses it off loopback). It
 * implements the same dashboardAuth boundary as the production adapter:
 * getAccessToken resolves the current dev user's token or rejects. The dev
 * token is a "dev-<name>" string verified by the local dev W3 stub; it is
 * kept in sessionStorage only and never mixed into generated dashboards.
 * Production deployments never set the flag and keep the fail-closed stub.
 */
window.dashboardAuth = Object.freeze({
  async getAccessToken() {
    const token = sessionStorage.getItem("dashboard-dev-token")
    if (token) return token
    throw new Error("AUTH_REQUIRED")
  },
  login() {
    const name = window.prompt(
      "开发模式登录：输入 dev 用户名（例如 alice）。\n" +
        "仅本机演示使用，凭据为 dev-<name>。",
    )
    if (!name) return
    const clean = name.trim().replace(/^dev-/, "")
    if (!/^[\w.-]{1,32}$/.test(clean)) {
      window.alert("开发用户名只能包含字母、数字、- _ .")
      return
    }
    sessionStorage.setItem("dashboard-dev-token", `dev-${clean}`)
    location.reload()
  },
  logout() {
    sessionStorage.removeItem("dashboard-dev-token")
    location.reload()
  },
})
