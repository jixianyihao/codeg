"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslations } from "next-intl"
import {
  Copy,
  ExternalLink,
  FilePenLine,
  LayoutDashboard,
  LockKeyhole,
  Plus,
  RefreshCw,
  Search,
  Settings2,
} from "lucide-react"
import { BrowserLink } from "@/components/ui/browser-link"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { WorkbenchPageTitle } from "@/components/workbench/workbench-page-title"
import {
  createDashboardMutation,
  DashboardApiError,
  dashboardViewUrl,
  listDashboards,
} from "@/lib/dashboard-api"
import type {
  DashboardPage,
  DashboardQuery,
  DashboardStatus,
} from "@/lib/dashboard-types"
import { getCodegToken } from "@/lib/transport/web-auth"
import { isDesktop } from "@/lib/platform"
import { DashboardManager } from "./dashboard-manager"
import {
  DASHBOARD_TAB_ACTIVE,
  DashboardError,
  DashboardMutationNotice,
  DashboardStatusBadge,
  useDashboardDate,
} from "./dashboard-common"
import {
  useDashboardMutation,
  reconcileDashboardMutationIdentity,
} from "./use-dashboard-mutation"

export function DashboardsPageTitle() {
  const t = useTranslations("Dashboards")
  return <WorkbenchPageTitle title={t("title")} />
}

export function DashboardsPage() {
  const t = useTranslations("Dashboards")
  const date = useDashboardDate()
  const [scope, setScope] = useState<DashboardQuery["scope"]>("all")
  const [status, setStatus] = useState<DashboardStatus>("published")
  const [search, setSearch] = useState("")
  const [query, setQuery] = useState("")
  const [page, setPage] = useState<DashboardPage | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [notice, setNotice] = useState("")
  const [selected, setSelected] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const request = useRef(0)
  const tokenAtLoad = useRef("")
  const principal = useRef("")
  const clearMutation = useRef(() => {})
  const desktop = isDesktop()

  const resetIdentity = useCallback(() => {
    ++request.current
    clearMutation.current()
    principal.current = ""
    setPage(null)
    setSelected(null)
    setCreating(false)
    setLoading(false)
    setError(new DashboardApiError("dashboard_identity_changed", 401))
  }, [])

  const load = useCallback(
    async (cursor?: string) => {
      if (desktop) return
      const ticket = ++request.current
      tokenAtLoad.current = getCodegToken()
      setLoading(true)
      setError(null)
      try {
        const next = await listDashboards({ scope, status, q: query, cursor })
        if (ticket !== request.current) return
        reconcileDashboardMutationIdentity(next.principal_id)
        if (principal.current && principal.current !== next.principal_id) {
          clearMutation.current()
          setSelected(null)
          setCreating(false)
        }
        principal.current = next.principal_id
        setPage((previous) => ({
          ...next,
          items:
            cursor &&
            previous?.principal_id === next.principal_id &&
            previous.service_origin === next.service_origin
              ? [
                  ...previous.items,
                  ...next.items.filter(
                    (item) => !previous.items.some((old) => old.id === item.id)
                  ),
                ]
              : next.items,
        }))
      } catch (cause) {
        if (ticket !== request.current) return
        setError(cause)
        if (cause instanceof DashboardApiError && cause.status === 401)
          resetIdentity()
        else if (!cursor) setPage(null)
      } finally {
        if (ticket === request.current) setLoading(false)
      }
    },
    [desktop, scope, status, query, resetIdentity]
  )

  const mutation = useDashboardMutation((operation) => {
    setRefreshKey((value) => value + 1)
    if (creating && operation.result?.dashboard_id) {
      setSelected(operation.result.dashboard_id)
      setCreating(false)
      setScope("mine")
      setStatus(operation.result.status ?? "draft")
    }
    void load()
  }, resetIdentity)
  useEffect(() => {
    clearMutation.current = mutation.clear
  }, [mutation.clear])

  useEffect(() => {
    if (desktop) return
    const params = new URLSearchParams(window.location.search)
    const id = params.get("dashboard")
    if (
      params.get("view") === "dashboards" &&
      id &&
      /^[a-zA-Z0-9_-]+$/.test(id)
    )
      setSelected(id)
  }, [desktop])

  useEffect(() => {
    void load()
    const refresh = () => {
      void load()
    }
    const authChanged = () => {
      if (tokenAtLoad.current !== getCodegToken()) {
        // Immediately hide prior identity's management and request state.
        resetIdentity()
        void load()
      }
    }
    window.addEventListener("focus", refresh)
    window.addEventListener("storage", authChanged)
    window.addEventListener("aresclaw:auth-changed", authChanged)
    window.addEventListener("aresclaw:dashboards-changed", refresh)
    return () => {
      // Request generation, not a DOM ref: invalidate every late response.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      ++request.current
      window.removeEventListener("focus", refresh)
      window.removeEventListener("storage", authChanged)
      window.removeEventListener("aresclaw:auth-changed", authChanged)
      window.removeEventListener("aresclaw:dashboards-changed", refresh)
    }
  }, [load, resetIdentity])

  const closeManager = () => {
    setSelected(null)
    const url = new URL(window.location.href)
    if (url.searchParams.has("dashboard")) {
      url.searchParams.delete("dashboard")
      window.history.replaceState(
        window.history.state,
        "",
        url.pathname + url.search + url.hash
      )
    }
  }

  if (desktop)
    return <p className="p-8 text-muted-foreground">{t("webOnly")}</p>

  return (
    <section className="flex h-full min-h-0 flex-col" aria-label={t("title")}>
      <div className="shrink-0 border-b px-5 py-4 sm:px-7">
        <div className="mx-auto max-w-7xl space-y-4">
          <header className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-3">
              <span className="flex size-10 shrink-0 items-center justify-center rounded-2xl border border-primary/10 bg-primary/5">
                <LayoutDashboard className="size-5 text-primary" />
              </span>
              <div>
                <h2 className="text-base font-semibold tracking-tight">
                  {t("title")}
                </h2>
                <p className="mt-0.5 text-xs leading-5 text-muted-foreground">
                  {t("description")}
                </p>
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label={t("refresh")}
                disabled={loading}
                onClick={() => void load()}
              >
                <RefreshCw
                  className={loading ? "size-4 animate-spin" : "size-4"}
                />
              </Button>
              <Button
                size="sm"
                disabled={!page?.identity || mutation.blocked}
                onClick={() => {
                  mutation.dismissFeedback()
                  setCreating(true)
                }}
              >
                <Plus className="size-3.5" />
                {t("create")}
              </Button>
            </div>
          </header>
          <div className="flex flex-wrap items-center gap-3">
            <Tabs
              value={scope}
              onValueChange={(value) =>
                setScope(value as DashboardQuery["scope"])
              }
            >
              <TabsList>
                {(["all", "mine", "shared"] as const).map((item) => (
                  <TabsTrigger
                    key={item}
                    value={item}
                    className={DASHBOARD_TAB_ACTIVE}
                  >
                    {t(
                      item === "all" && status !== "published"
                        ? "allStatuses"
                        : item
                    )}
                  </TabsTrigger>
                ))}
              </TabsList>
            </Tabs>
            <Select
              value={status}
              onValueChange={(value) => setStatus(value as DashboardStatus)}
            >
              <SelectTrigger size="sm" aria-label={t("status")}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(["published", "draft", "archived"] as const).map((item) => (
                  <SelectItem key={item} value={item}>
                    {t(item)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <form
              className="ml-auto flex min-w-48 flex-1 items-center gap-2 sm:max-w-80"
              onSubmit={(event) => {
                event.preventDefault()
                if (query === search.trim()) void load()
                else setQuery(search.trim())
              }}
            >
              <div className="relative min-w-0 flex-1">
                <Search className="pointer-events-none absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                <Input
                  aria-label={t("search")}
                  placeholder={t("search")}
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  className="h-8 pl-8 text-xs"
                />
              </div>
              <Button
                type="submit"
                variant="ghost"
                size="icon-sm"
                aria-label={t("search")}
              >
                <Search className="size-3.5" />
              </Button>
            </form>
          </div>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5 sm:px-7">
        <div className="mx-auto max-w-7xl space-y-4">
          <DashboardError error={error} />
          {page?.identity && !selected && !creating && (
            <DashboardMutationNotice mutation={mutation} />
          )}
          {notice && (
            <p role="status" className="text-xs text-muted-foreground">
              {notice}
            </p>
          )}
          {loading && !page && (
            <div
              role="status"
              aria-label={t("loading")}
              className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3"
            >
              {[0, 1, 2].map((item) => (
                <div
                  key={item}
                  className="h-56 animate-pulse rounded-2xl border bg-muted/30"
                />
              ))}
            </div>
          )}
          {!loading && !error && page?.items.length === 0 && (
            <div className="mx-auto flex max-w-md flex-col items-center py-20 text-center">
              <span className="mb-4 flex size-14 items-center justify-center rounded-2xl border border-dashed bg-muted/30">
                <LayoutDashboard className="size-6 text-muted-foreground" />
              </span>
              <p className="text-sm font-medium">{t("empty")}</p>
              <p className="mt-2 text-xs leading-5 text-muted-foreground">
                {t(status === "draft" ? "emptyDraftHelp" : "emptyHelp")}
              </p>
            </div>
          )}
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {page?.items.map((dashboard) => {
              const url =
                dashboard.status === "published" && dashboard.role
                  ? dashboardViewUrl(page.service_origin, dashboard.id)
                  : null
              const canManage =
                dashboard.role === "owner" || dashboard.role === "editor"
              return (
                <article
                  key={dashboard.id}
                  className="group flex min-w-0 flex-col overflow-hidden rounded-2xl border bg-card shadow-xs transition-[border-color,box-shadow] hover:border-primary/25 hover:shadow-md"
                >
                  <div className="flex items-center justify-between gap-2 px-5 pt-5">
                    <span className="flex size-9 items-center justify-center rounded-xl border bg-muted/35">
                      <LayoutDashboard className="size-4 text-muted-foreground" />
                    </span>
                    <DashboardStatusBadge status={dashboard.status} />
                  </div>
                  <div className="flex flex-1 flex-col px-5 pb-4 pt-4">
                    <h3 className="line-clamp-2 break-words text-[0.9375rem] font-semibold leading-6">
                      {dashboard.title}
                    </h3>
                    <p className="mt-1.5 line-clamp-2 min-h-10 text-xs leading-5 text-muted-foreground">
                      {dashboard.description || t("noDescription")}
                    </p>
                    {dashboard.has_draft && canManage && (
                      <p className="mt-3 flex items-center gap-1.5 text-[0.6875rem] text-amber-700 dark:text-amber-400">
                        <FilePenLine className="size-3" />
                        {t("pendingDraft", {
                          version: dashboard.draft_version_number ?? "—",
                        })}
                      </p>
                    )}
                    <div className="mt-auto space-y-2 pt-4">
                      <div className="flex min-w-0 items-center justify-between gap-3 text-[0.6875rem]">
                        <span className="truncate text-muted-foreground">
                          {dashboard.owner_name}
                          {dashboard.owner_type === "service"
                            ? ` · ${t("service")}`
                            : ""}
                        </span>
                        <span className="shrink-0 rounded-md bg-muted/60 px-1.5 py-0.5 text-[0.625rem] text-muted-foreground">
                          {t(dashboard.role ?? "noAccess")}
                        </span>
                      </div>
                      <div className="flex items-center justify-between gap-3 text-[0.625rem] text-muted-foreground">
                        <span>{date(dashboard.published_at)}</span>
                        {dashboard.current_version_number && (
                          <span>v{dashboard.current_version_number}</span>
                        )}
                      </div>
                      {dashboard.expires_at && (
                        <p className="text-[0.625rem] text-muted-foreground">
                          {t("expires")}: {date(dashboard.expires_at)}
                        </p>
                      )}
                    </div>
                  </div>
                  <div className="flex min-h-12 items-center justify-between gap-2 border-t bg-muted/10 px-4 py-2">
                    {url ? (
                      <BrowserLink
                        href={url}
                        className="inline-flex items-center gap-1.5 rounded-lg px-1 text-xs font-medium text-primary hover:underline"
                      >
                        {t("open")}
                        <ExternalLink className="size-3" />
                      </BrowserLink>
                    ) : (
                      <span className="inline-flex items-center gap-1.5 text-[0.6875rem] text-muted-foreground">
                        {!dashboard.role && <LockKeyhole className="size-3" />}
                        {t(
                          !dashboard.role
                            ? "noAccess"
                            : dashboard.status === "archived"
                              ? "offline"
                              : dashboard.status === "draft"
                                ? "notPublished"
                                : "invalidLink"
                        )}
                      </span>
                    )}
                    <div className="flex items-center gap-1">
                      {url && (
                        <Button
                          size="icon-sm"
                          variant="ghost"
                          aria-label={t("copy")}
                          onClick={async () => {
                            try {
                              await navigator.clipboard.writeText(url)
                              setNotice(t("copied"))
                            } catch {
                              setNotice(t("copyFailed"))
                            }
                          }}
                        >
                          <Copy className="size-3.5" />
                        </Button>
                      )}
                      {dashboard.role && (
                        <Button
                          size="sm"
                          variant="ghost"
                          className="h-7 gap-1 px-2 text-xs"
                          onClick={() => {
                            if (selected !== dashboard.id)
                              mutation.dismissFeedback()
                            setSelected(dashboard.id)
                          }}
                        >
                          {canManage && <Settings2 className="size-3.5" />}
                          {t(canManage ? "manage" : "details")}
                        </Button>
                      )}
                    </div>
                  </div>
                </article>
              )
            })}
          </div>
          {page?.next_cursor && (
            <div className="py-2 text-center">
              <Button
                size="sm"
                variant="outline"
                disabled={loading}
                onClick={() => void load(page.next_cursor ?? undefined)}
              >
                {t("more")}
              </Button>
            </div>
          )}
        </div>
      </div>
      {selected && page?.identity && (
        <DashboardManager
          key={`${page.principal_id}:${selected}`}
          id={selected}
          identity={page.identity}
          origin={page.service_origin}
          refreshKey={refreshKey}
          mutation={mutation}
          onClose={closeManager}
          onIdentityChanged={resetIdentity}
        />
      )}
      <Dialog open={creating} onOpenChange={setCreating}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("create")}</DialogTitle>
            <DialogDescription>{t("createHelp")}</DialogDescription>
          </DialogHeader>
          <DashboardMutationNotice mutation={mutation} />
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault()
              if (!page?.identity) return
              const data = new FormData(event.currentTarget)
              void mutation.perform(() =>
                createDashboardMutation(
                  page.identity,
                  "/dashboards/drafts",
                  "POST",
                  {
                    title: String(data.get("title")).trim(),
                    description: String(data.get("description")),
                  }
                )
              )
            }}
          >
            <fieldset className="space-y-4" disabled={mutation.blocked}>
              <label className="block space-y-1.5 text-xs font-medium">
                {t("titleLabel")}
                <Input name="title" required maxLength={200} autoFocus />
              </label>
              <label className="block space-y-1.5 text-xs font-medium">
                {t("descriptionLabel")}
                <Textarea name="description" maxLength={2000} rows={3} />
              </label>
              <Button type="submit" className="w-full">
                {t("createDraft")}
              </Button>
            </fieldset>
          </form>
        </DialogContent>
      </Dialog>
    </section>
  )
}
