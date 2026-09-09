export type DashboardRole = "owner" | "editor" | "viewer" | null
export type DashboardStatus = "draft" | "published" | "archived"
export type DashboardDisposition = "save_draft" | "publish"
export interface Dashboard {
  id: string
  title: string
  description: string
  owner_principal_id: string
  owner_name: string
  owner_type: "human" | "service"
  current_version_id: string | null
  current_version_number: number | null
  current_version_sha256?: string | null
  current_version_byte_size?: number | null
  draft_version_id?: string | null
  draft_version_number?: number | null
  draft_version_sha256?: string | null
  draft_version_byte_size?: number | null
  has_draft?: boolean
  revision: number
  status: DashboardStatus
  role: DashboardRole
  created_at: string
  updated_at: string
  published_at: string | null
  expires_at: string | null
}
export interface DashboardIdentity {
  principal_id: string
  principal_type: "human"
  display_name: string
  scopes: string[]
}
export interface DashboardPage {
  items: Dashboard[]
  next_cursor: string | null
  principal_id: string
  service_origin: string
  identity: DashboardIdentity
}
export interface DashboardQuery {
  scope: "mine" | "shared" | "all"
  q?: string
  cursor?: string
  status?: DashboardStatus
}
export interface DashboardVersion {
  id: string
  number: number
  sha256: string
  byte_size: number
  created_at: string
  created_by: string
  published_at: string | null
}
export interface DashboardVersions {
  items: DashboardVersion[]
  next_cursor: string | null
}
export type DashboardSubjectType = "user" | "group" | "service"
export interface DashboardPrincipal {
  id: string
  type: DashboardSubjectType
  display_name: string
}
export interface DashboardGrant {
  subject_type: DashboardSubjectType | "all_authenticated"
  subject_id: string
  subject_name?: string | null
  role: "viewer" | "editor"
  starts_at: string | null
  expires_at: string | null
}
export interface DashboardOperation {
  state: "succeeded" | "failed" | "accepted" | "processing"
  operation_id: string
  idempotency_key: string
  result: {
    dashboard_id?: string
    revision?: number
    status?: DashboardStatus
    version_id?: string
    disposition?: DashboardDisposition
  } | null
  error: { code: string; message?: string; retryable?: boolean } | null
}
// Memory only. Contains frozen request bytes, never a token. The browser-window
// store retains it across management-panel and workbench-route changes.
export interface DashboardMutation {
  readonly id: string
  readonly principal_id: string
  readonly service_origin: string
  readonly path: string
  readonly method: "POST" | "PATCH" | "PUT" | "DELETE"
  readonly body: string | FormData | undefined
}
