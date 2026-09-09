/* Deployment integration point: replace this file with the approved W3 browser
 * adapter. getAccessToken() must resolve the CURRENT human's short-lived access
 * token, or reject when login is required. Keep tokens in memory; never use a
 * machine credential, URL parameter, localStorage, or an embedded static secret.
 * The backend verifies W3 independently. This default intentionally fails closed.
 */
window.dashboardAuth =
  window.dashboardAuth ||
  Object.freeze({
    async getAccessToken() {
      throw new Error("AUTH_NOT_CONFIGURED")
    },
  })
