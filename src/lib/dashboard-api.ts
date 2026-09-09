import { getCodegToken } from "@/lib/transport/web-auth"
import type {
  Dashboard,
  DashboardPage,
  DashboardQuery,
} from "./dashboard-types"

// Same-origin reverse proxy route (deployment-configured):
//   AresClaw /dashboard-api/v1/*  →  dashboard service /api/v1/*
// The browser uses the existing web credential only; it never reads the
// Agent environment's auth_token file, and no Rust handler is involved.
const API_BASE = "/dashboard-api/v1"

// Stable detail links come from this build-time control origin (operators
// configure it at deployment); cards never execute service-returned URLs.
const CONTROL_ORIGIN = process.env.NEXT_PUBLIC_DASHBOARD_CONTROL_ORIGIN ?? ""

async function apiFetch(path: string): Promise<unknown> {
  const token = getCodegToken()
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: "omit",
    cache: "no-store",
    headers: {
      Accept: "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  })
  if (token !== getCodegToken()) throw new Error("dashboard_identity_changed")
  if (response.status === 401) throw new Error("dashboard_unauthorized")
  if (!response.ok)
    throw new Error(`dashboard_service_error_${response.status}`)
  const body = await response.json()
  if (token !== getCodegToken()) throw new Error("dashboard_identity_changed")
  return body
}

function isDashboard(value: unknown): value is Dashboard {
  if (typeof value !== "object" || value === null) return false
  const item = value as Record<string, unknown>
  return (
    typeof item.id === "string" &&
    typeof item.title === "string" &&
    typeof item.revision === "number" &&
    (item.status === "published" || item.status === "archived")
  )
}

export async function listDashboards(
  query: DashboardQuery
): Promise<DashboardPage> {
  const params = new URLSearchParams()
  params.set("scope", query.scope)
  if (query.q) params.set("q", query.q)
  if (query.cursor) params.set("cursor", query.cursor)
  if (query.status) params.set("status", query.status)
  const [me, page] = await Promise.all([
    apiFetch("/me") as Promise<Record<string, unknown>>,
    apiFetch(`/dashboards?${params.toString()}`) as Promise<
      Record<string, unknown>
    >,
  ])
  const items = Array.isArray(page.items) ? page.items.filter(isDashboard) : []
  return {
    items,
    next_cursor: typeof page.next_cursor === "string" ? page.next_cursor : null,
    principal_id: typeof me.principal_id === "string" ? me.principal_id : "",
    service_origin: CONTROL_ORIGIN,
  }
}

export function dashboardViewUrl(origin: string, id: string): string | null {
  try {
    if (!/^[a-zA-Z0-9_-]+$/.test(id)) return null
    if (!origin) return null
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
