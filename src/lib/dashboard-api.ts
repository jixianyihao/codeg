import { getTransport } from "@/lib/transport"
import { getCodegToken } from "@/lib/transport/web-auth"
import type { DashboardPage, DashboardQuery } from "./dashboard-types"

export async function listDashboards(
  query: DashboardQuery
): Promise<DashboardPage> {
  const identity = getCodegToken()
  const page = await getTransport().call<DashboardPage>("dashboard_list", {
    ...query,
  })
  if (identity !== getCodegToken())
    throw new Error("dashboard_identity_changed")
  return page
}

export function dashboardViewUrl(origin: string, id: string): string | null {
  try {
    if (!/^[a-zA-Z0-9_-]+$/.test(id)) return null
    const url = new URL(origin)
    const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
    if (url.protocol !== "https:" && !(loopback && url.protocol === "http:"))
      return null
    if (
      url.username ||
      url.password ||
      url.search ||
      url.hash ||
      url.pathname !== "/"
    )
      return null
    return new URL(`/dashboards/${id}`, url.origin).href
  } catch {
    return null
  }
}
