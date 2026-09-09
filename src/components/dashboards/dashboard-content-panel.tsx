"use client"

import { useEffect, useRef, useState } from "react"
import { useTranslations } from "next-intl"
import {
  Download,
  ExternalLink,
  FileCode2,
  History,
  Loader2,
  Upload,
} from "lucide-react"
import { BrowserLink } from "@/components/ui/browser-link"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  createDashboardMutation,
  createDashboardUpload,
  dashboardViewUrl,
  getDashboardSource,
  listDashboardVersions,
} from "@/lib/dashboard-api"
import type {
  Dashboard,
  DashboardDisposition,
  DashboardIdentity,
  DashboardVersions,
} from "@/lib/dashboard-types"
import { DashboardError, useDashboardDate } from "./dashboard-common"
import {
  DashboardConfirm,
  type DashboardConfirmation,
} from "./dashboard-confirm"
import type { DashboardMutationController } from "./use-dashboard-mutation"

export function DashboardContentPanel({
  dashboard,
  identity,
  origin,
  mutation,
  canEdit,
  canPublish,
  accessSummary,
}: {
  dashboard: Dashboard
  identity: DashboardIdentity
  origin: string
  mutation: DashboardMutationController
  canEdit: boolean
  canPublish: boolean
  accessSummary: string
}) {
  const t = useTranslations("Dashboards")
  const date = useDashboardDate()
  const [versions, setVersions] = useState<DashboardVersions | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(false)
  const [downloading, setDownloading] = useState<string | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [confirmation, setConfirmation] =
    useState<DashboardConfirmation | null>(null)
  const generation = useRef(0)
  const active = useRef(true)
  const archived = dashboard.status === "archived"
  const base = `/dashboards/${encodeURIComponent(dashboard.id)}`
  const canView = !archived && !!dashboard.role

  async function load(cursor?: string) {
    const ticket = ++generation.current
    setLoading(true)
    setError(null)
    try {
      const page = await listDashboardVersions(dashboard.id, cursor)
      if (!active.current || ticket !== generation.current) return
      setVersions((previous) => ({
        ...page,
        items:
          cursor && previous
            ? [
                ...previous.items,
                ...page.items.filter(
                  (item) => !previous.items.some((old) => old.id === item.id)
                ),
              ]
            : page.items,
      }))
    } catch (cause) {
      if (active.current && ticket === generation.current) setError(cause)
    } finally {
      if (active.current && ticket === generation.current) setLoading(false)
    }
  }
  useEffect(() => {
    active.current = true
    void load()
    return () => {
      active.current = false
      // Request generation, not a DOM ref: invalidate every late response.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      ++generation.current
    }
    // The parent keys this panel by board ID + revision.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function publishDescription() {
    return `${t("publishImpact", { version: dashboard.current_version_number ?? "—" })}\n${accessSummary}`
  }
  function upload(disposition: DashboardDisposition) {
    if (!file) return
    const snapshot = file
    const action = () => {
      void mutation.perform(() =>
        createDashboardUpload(identity, dashboard, snapshot, disposition)
      )
    }
    if (disposition === "publish")
      setConfirmation({
        title: t("publishUpload"),
        description: publishDescription(),
        action,
      })
    else action()
  }
  async function download(versionId: string) {
    setDownloading(versionId)
    setError(null)
    try {
      const blob = await getDashboardSource(dashboard.id, versionId)
      if (!active.current) return
      const href = URL.createObjectURL(blob)
      const link = document.createElement("a")
      link.href = href
      link.download = `${dashboard.id}-${versionId}.txt`
      document.body.append(link)
      link.click()
      link.remove()
      setTimeout(() => URL.revokeObjectURL(href), 1000)
    } catch (cause) {
      if (active.current) setError(cause)
    } finally {
      if (active.current) setDownloading(null)
    }
  }
  function viewLink(versionId: string | null, label: string) {
    const href =
      versionId && canView
        ? dashboardViewUrl(origin, dashboard.id, versionId)
        : null
    return href ? (
      <BrowserLink
        href={href}
        className="inline-flex items-center gap-1.5 text-xs font-medium text-primary hover:underline"
      >
        {label}
        <ExternalLink className="size-3" />
      </BrowserLink>
    ) : null
  }

  return (
    <div className="space-y-5">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-3 rounded-2xl border bg-muted/20 p-4">
          <p className="text-[0.6875rem] font-medium text-muted-foreground">
            {t("liveVersion")}
          </p>
          <p className="text-xl font-semibold tracking-tight">
            {dashboard.current_version_number
              ? `v${dashboard.current_version_number}`
              : "—"}
          </p>
          <p className="text-[0.6875rem] text-muted-foreground">
            {dashboard.status === "published"
              ? date(dashboard.published_at)
              : t("offline")}
          </p>
          {dashboard.status === "published" &&
            viewLink(dashboard.current_version_id, t("open"))}
        </div>
        {canEdit && (
          <div className="space-y-3 rounded-2xl border border-amber-500/20 bg-amber-500/5 p-4">
            <p className="text-[0.6875rem] font-medium text-amber-700 dark:text-amber-400">
              {t("draftVersion")}
            </p>
            <p className="text-xl font-semibold tracking-tight">
              {dashboard.draft_version_number
                ? `v${dashboard.draft_version_number}`
                : "—"}
            </p>
            <p className="text-[0.6875rem] text-muted-foreground">
              {dashboard.draft_version_id ? t("draftPrivate") : t("noDraft")}
            </p>
            {viewLink(dashboard.draft_version_id ?? null, t("previewDraft"))}
            {dashboard.draft_version_id && canPublish && (
              <Button
                size="sm"
                className="w-full"
                disabled={mutation.blocked}
                onClick={() =>
                  setConfirmation({
                    title: t("publishDraft"),
                    description: publishDescription(),
                    action: () => {
                      void mutation.perform(() =>
                        createDashboardMutation(
                          identity,
                          `${base}/publish`,
                          "POST",
                          {
                            version_id: dashboard.draft_version_id,
                            expected_revision: dashboard.revision,
                          }
                        )
                      )
                    },
                  })
                }
              >
                {t("publishDraft")}
              </Button>
            )}
          </div>
        )}
      </div>
      {archived && (
        <p className="rounded-xl bg-muted/40 p-3 text-xs leading-5 text-muted-foreground">
          {t("archivedHelp")}
        </p>
      )}
      {canEdit && !archived && (
        <section
          className="space-y-3 rounded-2xl border p-4"
          aria-label={t("uploadTitle")}
        >
          <div className="flex items-center gap-2">
            <Upload className="size-4 text-muted-foreground" />
            <h3 className="text-sm font-medium">{t("uploadTitle")}</h3>
          </div>
          <p className="text-xs leading-5 text-muted-foreground">
            {t("uploadHelp")}
          </p>
          <label className="block space-y-2 text-xs font-medium">
            {t("htmlFile")}
            <Input
              type="file"
              accept=".html,.htm,text/html"
              disabled={mutation.blocked}
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              className="text-xs"
            />
          </label>
          {file && (
            <p className="break-all text-xs text-muted-foreground">
              {file.name} · {(file.size / 1024).toFixed(1)} KiB
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={!file || mutation.blocked}
              onClick={() => upload("save_draft")}
            >
              {t("saveDraft")}
            </Button>
            {canPublish && (
              <Button
                size="sm"
                disabled={!file || mutation.blocked}
                onClick={() => upload("publish")}
              >
                {t("publishUpload")}
              </Button>
            )}
          </div>
          {!canPublish && (
            <p className="text-xs text-muted-foreground">
              {t("ownerPublishes")}
            </p>
          )}
        </section>
      )}
      <section aria-label={t("versions")} className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <h3 className="flex items-center gap-2 text-sm font-medium">
            <History className="size-4 text-muted-foreground" />
            {t("versions")}
          </h3>
          <Button
            size="sm"
            variant="ghost"
            disabled={loading}
            onClick={() => void load()}
          >
            {t("refresh")}
          </Button>
        </div>
        <p className="text-xs leading-5 text-muted-foreground">
          {t("versionHelp")}
        </p>
        <DashboardError error={error} />
        {loading && !versions && (
          <p role="status" className="text-xs text-muted-foreground">
            {t("loading")}
          </p>
        )}
        {versions?.items.length === 0 && (
          <p className="rounded-xl border border-dashed p-5 text-center text-xs text-muted-foreground">
            {t("noVersions")}
          </p>
        )}
        <div className="divide-y rounded-xl border empty:hidden">
          {versions?.items.map((version) => (
            <div key={version.id} className="space-y-3 p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="flex min-w-0 items-center gap-2">
                  <FileCode2 className="size-4 shrink-0 text-muted-foreground" />
                  <span className="text-sm font-semibold">
                    v{version.number}
                  </span>
                  <span className="text-[0.625rem] text-muted-foreground">
                    {version.id === dashboard.current_version_id &&
                    dashboard.status === "published"
                      ? t("current")
                      : version.published_at
                        ? t("releasedVersion")
                        : t("unpublishedVersion")}
                  </span>
                </div>
                <span className="shrink-0 text-[0.625rem] text-muted-foreground">
                  {(version.byte_size / 1024).toFixed(1)} KiB
                </span>
              </div>
              <p className="text-[0.6875rem] text-muted-foreground">
                {date(version.created_at)}
              </p>
              {canView && (
                <div className="flex flex-wrap items-center gap-3">
                  {viewLink(
                    version.id,
                    t("previewVersion", { number: version.number })
                  )}
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7 px-1 text-xs"
                    disabled={!!downloading}
                    aria-label={t("sourceVersion", { number: version.number })}
                    onClick={() => void download(version.id)}
                  >
                    {downloading === version.id ? (
                      <Loader2 className="size-3 animate-spin" />
                    ) : (
                      <Download className="size-3" />
                    )}
                    {t("source")}
                  </Button>
                  {canEdit &&
                    dashboard.status === "published" &&
                    version.published_at &&
                    version.id !== dashboard.current_version_id && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-7 px-1 text-xs"
                        disabled={mutation.blocked}
                        aria-label={t("rollbackVersion", {
                          number: version.number,
                        })}
                        onClick={() =>
                          setConfirmation({
                            title: t("rollbackVersion", {
                              number: version.number,
                            }),
                            description: t("rollbackImpact"),
                            action: () => {
                              void mutation.perform(() =>
                                createDashboardMutation(
                                  identity,
                                  `${base}/rollback`,
                                  "POST",
                                  {
                                    version_id: version.id,
                                    expected_revision: dashboard.revision,
                                  }
                                )
                              )
                            },
                          })
                        }
                      >
                        {t("rollback")}
                      </Button>
                    )}
                </div>
              )}
            </div>
          ))}
        </div>
        {versions?.next_cursor && (
          <Button
            size="sm"
            variant="outline"
            disabled={loading}
            onClick={() => void load(versions.next_cursor!)}
          >
            {t("more")}
          </Button>
        )}
      </section>
      <DashboardConfirm
        confirmation={confirmation}
        onClose={() => setConfirmation(null)}
      />
    </div>
  )
}
