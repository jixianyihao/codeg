"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { useLocale, useTranslations } from "next-intl"
import {
  Copy,
  ExternalLink,
  LayoutDashboard,
  RefreshCw,
  Search,
} from "lucide-react"
import { BrowserLink } from "@/components/ui/browser-link"
import { Button } from "@/components/ui/button"
import { WorkbenchPageTitle } from "@/components/workbench/workbench-page-title"
import { dashboardViewUrl, listDashboards } from "@/lib/dashboard-api"
import type { DashboardPage, DashboardQuery } from "@/lib/dashboard-types"
import { getCodegToken } from "@/lib/transport/web-auth"
import { isDesktop } from "@/lib/platform"

export function DashboardsPageTitle() {
  const t = useTranslations("Dashboards")
  return <WorkbenchPageTitle title={t("title")} />
}

export function DashboardsPage() {
  const t = useTranslations("Dashboards")
  const locale = useLocale()
  const [scope, setScope] = useState<DashboardQuery["scope"]>("all")
  const [status, setStatus] = useState<"published" | "archived">("published")
  const [search, setSearch] = useState("")
  const [query, setQuery] = useState("")
  const [page, setPage] = useState<DashboardPage | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<"error" | "notConfigured" | null>(null)
  const [notice, setNotice] = useState("")
  const request = useRef(0)
  const identity = useRef("")
  const desktop = isDesktop()

  const load = useCallback(
    async (cursor?: string) => {
      if (desktop) return
      const ticket = ++request.current
      identity.current = getCodegToken()
      setLoading(true)
      setError(null)
      setNotice("")
      if (!cursor) setPage(null)
      try {
        const next = await listDashboards({ scope, status, q: query, cursor })
        if (ticket !== request.current) return
        setPage((previous) => ({
          ...next,
          items:
            cursor &&
            previous?.principal_id === next.principal_id &&
            previous.service_origin === next.service_origin
              ? [...previous.items, ...next.items]
              : next.items,
        }))
      } catch (cause) {
        if (ticket !== request.current) return
        setPage(null)
        const message = cause instanceof Error ? cause.message : ""
        setError(
          message.includes("dashboard_not_configured")
            ? "notConfigured"
            : ("error" as const)
        )
      } finally {
        if (ticket === request.current) setLoading(false)
      }
    },
    [desktop, scope, status, query]
  )

  useEffect(() => {
    void load()
    const refresh = () => {
      void load()
    }
    const authChanged = () => {
      if (identity.current !== getCodegToken()) {
        ++request.current
        setPage(null)
        void load()
      }
    }
    window.addEventListener("focus", refresh)
    window.addEventListener("storage", authChanged)
    window.addEventListener("aresclaw:auth-changed", authChanged)
    window.addEventListener("aresclaw:dashboards-changed", refresh)
    const requestAtMount = request.current
    return () => {
      // Mutable staleness counter, not a DOM node: safe to bump in cleanup.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      if (request.current === requestAtMount) ++request.current
      window.removeEventListener("focus", refresh)
      window.removeEventListener("storage", authChanged)
      window.removeEventListener("aresclaw:auth-changed", authChanged)
      window.removeEventListener("aresclaw:dashboards-changed", refresh)
    }
  }, [load])

  const date = (value: string) => {
    const parsed = new Date(value)
    return Number.isNaN(parsed.getTime())
      ? "—"
      : new Intl.DateTimeFormat(locale, {
          dateStyle: "medium",
          timeStyle: "short",
        }).format(parsed)
  }

  if (desktop)
    return <p className="p-8 text-muted-foreground">{t("webOnly")}</p>

  return (
    <section
      className="h-full overflow-auto px-5 py-6 sm:px-8"
      aria-label={t("title")}
    >
      <div className="mx-auto max-w-6xl space-y-6">
        <header className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-2xl font-semibold tracking-tight">
              {t("title")}
            </h2>
            <p className="mt-1 text-sm text-muted-foreground">
              {t("description")}
            </p>
          </div>
          <Button
            variant="outline"
            onClick={() => void load()}
            disabled={loading}
          >
            <RefreshCw className={loading ? "size-4 animate-spin" : "size-4"} />
            {t("refresh")}
          </Button>
        </header>
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex rounded-lg border p-1" aria-label={t("title")}>
            {(["mine", "shared", "all"] as const).map((item) => (
              <Button
                key={item}
                size="sm"
                variant={scope === item ? "secondary" : "ghost"}
                aria-pressed={scope === item}
                onClick={() => setScope(item)}
              >
                {t(item)}
              </Button>
            ))}
          </div>
          <select
            aria-label={t("status")}
            value={status}
            onChange={(event) => setStatus(event.target.value as typeof status)}
            className="h-9 rounded-lg border bg-background px-3 text-sm"
          >
            <option value="published">{t("published")}</option>
            <option value="archived">{t("archived")}</option>
          </select>
          <form
            className="flex min-w-48 flex-1 items-center gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              if (query === search.trim()) void load()
              else setQuery(search.trim())
            }}
          >
            <input
              className="h-9 min-w-0 flex-1 rounded-lg border bg-background px-3 text-sm"
              aria-label={t("search")}
              placeholder={t("search")}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            <Button
              type="submit"
              variant="outline"
              size="icon"
              aria-label={t("search")}
            >
              <Search className="size-4" />
            </Button>
          </form>
        </div>
        {error ? (
          <p
            role="alert"
            className="rounded-xl border border-destructive/30 p-5 text-sm text-destructive"
          >
            {t(error)}
          </p>
        ) : null}
        {notice && (
          <p role="status" className="text-sm text-muted-foreground">
            {notice}
          </p>
        )}
        {loading && !page && (
          <p role="status" className="py-12 text-center text-muted-foreground">
            {t("loading")}
          </p>
        )}
        {!loading && !error && page?.items.length === 0 && (
          <div className="rounded-xl border border-dashed py-16 text-center text-muted-foreground">
            <LayoutDashboard className="mx-auto mb-3 size-8 opacity-50" />
            {t("empty")}
          </div>
        )}
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {page?.items.map((dashboard) => {
            const url = dashboardViewUrl(page.service_origin, dashboard.id)
            return (
              <article
                key={dashboard.id}
                className="flex min-w-0 flex-col rounded-xl border bg-card p-5 shadow-sm"
              >
                <div className="mb-4 flex items-center justify-between gap-2">
                  <LayoutDashboard className="size-5 text-muted-foreground" />
                  <span className="rounded-md bg-secondary px-2 py-1 text-xs">
                    {t(dashboard.role ?? "noAccess")}
                  </span>
                </div>
                <h3 className="break-words text-base font-semibold">
                  {dashboard.title}
                </h3>
                <p className="mt-2 line-clamp-3 flex-1 text-sm text-muted-foreground">
                  {dashboard.description}
                </p>
                <dl className="my-5 space-y-1 text-xs text-muted-foreground">
                  <div>
                    {dashboard.owner_name}
                    {dashboard.owner_type === "service" && ` · ${t("service")}`}
                  </div>
                  <div>
                    {t("publishedAt")}: {date(dashboard.published_at)}
                  </div>
                  {dashboard.expires_at && (
                    <div>
                      {t("expires")}: {date(dashboard.expires_at)}
                    </div>
                  )}
                  {dashboard.status === "archived" && (
                    <div>{t("archived")}</div>
                  )}
                </dl>
                {url ? (
                  <div className="flex items-center justify-between gap-2 border-t pt-3">
                    <BrowserLink
                      className="inline-flex items-center gap-2 text-sm font-medium hover:underline"
                      href={url}
                    >
                      {t("open")}
                      <ExternalLink className="size-3.5" />
                    </BrowserLink>
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={t("copy")}
                      onClick={async () => {
                        try {
                          await navigator.clipboard.writeText(url)
                          setNotice(t("copied"))
                        } catch {
                          setNotice(t("error"))
                        }
                      }}
                    >
                      <Copy className="size-4" />
                    </Button>
                  </div>
                ) : (
                  <p className="text-xs text-destructive">{t("invalidLink")}</p>
                )}
              </article>
            )
          })}
        </div>
        {page?.next_cursor && (
          <div className="text-center">
            <Button
              variant="outline"
              disabled={loading}
              onClick={() => void load(page.next_cursor ?? undefined)}
            >
              {t("more")}
            </Button>
          </div>
        )}
      </div>
    </section>
  )
}
