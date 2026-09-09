export type DashboardRole = "owner" | "editor" | "viewer"
export interface Dashboard {
  id: string
  title: string
  description: string
  owner_principal_id: string
  owner_name: string
  owner_type: "human" | "service"
  current_version_id: string
  revision: number
  status: "published" | "archived"
  role: DashboardRole
  published_at: string
  expires_at: string | null
}

export interface DashboardPage {
  items: Dashboard[]
  next_cursor: string | null
  principal_id: string
  service_origin: string
}

export interface DashboardQuery {
  scope: "mine" | "shared" | "all"
  q?: string
  cursor?: string
  status?: "published" | "archived"
}
