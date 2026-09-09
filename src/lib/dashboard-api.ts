import { getCodegToken } from "@/lib/transport/web-auth"
import type {
  Dashboard,
  DashboardDisposition,
  DashboardGrant,
  DashboardIdentity,
  DashboardMutation,
  DashboardOperation,
  DashboardPage,
  DashboardPrincipal,
  DashboardQuery,
  DashboardSubjectType,
  DashboardVersions,
} from "./dashboard-types"

// Ingress forwards this same-origin route to the independent service. This
// feature does not introduce a Rust proxy or read the CLI's token file.
const API_BASE = "/dashboard-api/v1"
const CONTROL_ORIGIN = process.env.NEXT_PUBLIC_DASHBOARD_CONTROL_ORIGIN ?? ""
export const DASHBOARD_MAX_BYTES = 10 * 1024 * 1024

export class DashboardApiError extends Error {
  constructor(
    public readonly code: string,
    public readonly status = 0,
    public readonly traceId?: string
  ) {
    super(code)
  }
  get uncertain() {
    return this.status === 0 || this.status >= 500 || this.status === 408
  }
}

async function apiResponse(
  path: string,
  init: RequestInit = {},
  expectedToken?: string
) {
  if (!path.startsWith("/") || path.startsWith("//"))
    throw new DashboardApiError("invalid_input", 422)
  const token = getCodegToken()
  if (expectedToken !== undefined && expectedToken !== token)
    throw new DashboardApiError("dashboard_identity_changed", 401)
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
      signal: AbortSignal.timeout(30000),
      headers: {
        Accept: "application/json",
        "X-Dashboard-Auth-Mode": "human",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...init.headers,
      },
    })
  } catch {
    if (token !== getCodegToken())
      throw new DashboardApiError("dashboard_identity_changed", 401)
    throw new DashboardApiError("network_outcome_unknown")
  }
  if (token !== getCodegToken())
    throw new DashboardApiError("dashboard_identity_changed", 401)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    if (token !== getCodegToken())
      throw new DashboardApiError("dashboard_identity_changed", 401)
    throw new DashboardApiError(
      response.status === 401
        ? "dashboard_unauthorized"
        : body.code || "service_error",
      response.status,
      body.trace_id
    )
  }
  return { response, token }
}

async function apiFetch<T>(
  path: string,
  init?: RequestInit,
  expectedToken?: string
): Promise<T> {
  const { response, token } = await apiResponse(path, init, expectedToken)
  let body: T
  try {
    body = await response.json()
  } catch {
    throw new DashboardApiError("invalid_response")
  }
  if (token !== getCodegToken())
    throw new DashboardApiError("dashboard_identity_changed", 401)
  return body
}

export async function getDashboardIdentity(): Promise<DashboardIdentity> {
  const me = await apiFetch<DashboardIdentity>("/me")
  if (
    !me ||
    typeof me.principal_id !== "string" ||
    me.principal_type !== "human"
  )
    throw new DashboardApiError("human_required", 403)
  return { ...me, scopes: Array.isArray(me.scopes) ? me.scopes : [] }
}

function isDashboard(value: unknown): value is Dashboard {
  if (typeof value !== "object" || value === null) return false
  const item = value as Record<string, unknown>
  return (
    typeof item.id === "string" &&
    typeof item.title === "string" &&
    typeof item.revision === "number" &&
    ["published", "draft", "archived"].includes(String(item.status))
  )
}

export async function listDashboards(
  query: DashboardQuery
): Promise<DashboardPage> {
  const params = new URLSearchParams({ scope: query.scope })
  if (query.q) params.set("q", query.q)
  if (query.cursor) params.set("cursor", query.cursor)
  if (query.status) params.set("status", query.status)
  const [identity, page] = await Promise.all([
    getDashboardIdentity(),
    apiFetch<{ items: unknown[]; next_cursor: string | null }>(
      `/dashboards?${params}`
    ),
  ])
  return {
    items: Array.isArray(page.items) ? page.items.filter(isDashboard) : [],
    next_cursor: typeof page.next_cursor === "string" ? page.next_cursor : null,
    principal_id: identity.principal_id,
    identity,
    service_origin: CONTROL_ORIGIN,
  }
}

const idPath = (id: string) => `/dashboards/${encodeURIComponent(id)}`
export const getDashboard = (id: string) => apiFetch<Dashboard>(idPath(id))
export const listDashboardVersions = (id: string, cursor?: string) =>
  apiFetch<DashboardVersions>(
    `${idPath(id)}/versions${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`
  )
export const listDashboardGrants = (id: string) =>
  apiFetch<{ items: DashboardGrant[]; revision: number }>(
    `${idPath(id)}/grants`
  )
export const getDashboardAccess = (id: string) =>
  apiFetch<{ role: string | null; sources: DashboardGrant[] }>(
    `${idPath(id)}/access`
  )
export const searchDashboardPrincipals = (
  type: DashboardSubjectType,
  q: string,
  cursor?: string
) => {
  const params = new URLSearchParams({ type, q })
  if (cursor) params.set("cursor", cursor)
  return apiFetch<{ items: DashboardPrincipal[]; next_cursor: string | null }>(
    `/principals?${params}`
  )
}

export function createDashboardMutation(
  identity: DashboardIdentity,
  path: string,
  method: DashboardMutation["method"],
  payload?: Record<string, unknown>
): DashboardMutation {
  return Object.freeze({
    id: crypto.randomUUID(),
    principal_id: identity.principal_id,
    service_origin: window.location.origin,
    path,
    method,
    body: payload === undefined ? undefined : JSON.stringify(payload),
  })
}

export async function createDashboardUpload(
  identity: DashboardIdentity,
  dashboard: Dashboard,
  file: File,
  disposition: DashboardDisposition
): Promise<DashboardMutation> {
  if (
    !/\.html?$/i.test(file.name) ||
    !file.size ||
    file.size > DASHBOARD_MAX_BYTES
  )
    throw new DashboardApiError("invalid_html_file", 422)
  const bytes = await file.arrayBuffer()
  try {
    new TextDecoder("utf-8", { fatal: true }).decode(bytes)
  } catch {
    throw new DashboardApiError("invalid_html_file", 422)
  }
  const digest = await crypto.subtle.digest("SHA-256", bytes)
  const sha256 = Array.from(new Uint8Array(digest), (b) =>
    b.toString(16).padStart(2, "0")
  ).join("")
  const body = new FormData()
  // Metadata MUST precede the single HTML part. Omitting title/description
  // preserves current metadata instead of unexpectedly clearing it.
  body.append(
    "metadata",
    JSON.stringify({
      expected_revision: dashboard.revision,
      disposition,
      byte_size: bytes.byteLength,
      content_sha256: sha256,
    })
  )
  body.append("html", new Blob([bytes], { type: "text/html" }), file.name)
  return Object.freeze({
    ...createDashboardMutation(
      identity,
      `${idPath(dashboard.id)}/versions`,
      "POST"
    ),
    body,
  })
}

async function verifyMutationIdentity(request: DashboardMutation) {
  if (request.service_origin !== window.location.origin)
    throw new DashboardApiError("dashboard_identity_changed", 401)
  const token = getCodegToken()
  const identity = await getDashboardIdentity()
  if (token !== getCodegToken())
    throw new DashboardApiError("dashboard_identity_changed", 401)
  if (identity.principal_id !== request.principal_id)
    throw new DashboardApiError("dashboard_identity_changed", 401)
  return token
}

function operation(value: DashboardOperation): DashboardOperation {
  if (
    !value ||
    !["succeeded", "failed", "accepted", "processing"].includes(value.state)
  )
    throw new DashboardApiError("invalid_response")
  return value
}

export async function executeDashboardMutation(request: DashboardMutation) {
  const token = await verifyMutationIdentity(request)
  return operation(
    await apiFetch<DashboardOperation>(
      request.path,
      {
        method: request.method,
        body: request.body,
        headers: {
          "Idempotency-Key": request.id,
          ...(typeof request.body === "string"
            ? { "Content-Type": "application/json" }
            : {}),
        },
      },
      token
    )
  )
}
export async function getDashboardOperation(request: DashboardMutation) {
  const token = await verifyMutationIdentity(request)
  return operation(
    await apiFetch<DashboardOperation>(
      `/operations?request_id=${encodeURIComponent(request.id)}`,
      undefined,
      token
    )
  )
}
export async function getDashboardSource(
  id: string,
  versionId: string
): Promise<Blob> {
  const { response, token } = await apiResponse(
    `${idPath(id)}/versions/${encodeURIComponent(versionId)}/source`
  )
  const content = await response.arrayBuffer()
  if (token !== getCodegToken())
    throw new DashboardApiError("dashboard_identity_changed", 401)
  if (content.byteLength > DASHBOARD_MAX_BYTES)
    throw new DashboardApiError("upload_too_large", 413)
  // Always download inert source bytes; never open a blob containing HTML.
  return new Blob([content], { type: "text/plain;charset=utf-8" })
}

export function dashboardViewUrl(
  origin: string,
  id: string,
  versionId?: string
): string | null {
  try {
    if (
      !/^[a-zA-Z0-9_-]+$/.test(id) ||
      !origin ||
      (versionId && !/^[a-zA-Z0-9_-]+$/.test(versionId))
    )
      return null
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
    const view = new URL(`/dashboards/${id}`, url.origin)
    if (versionId) view.searchParams.set("version", versionId)
    return view.href
  } catch {
    return null
  }
}
