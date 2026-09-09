"use client"

import { useEffect, useRef, useState } from "react"
import { useTranslations } from "next-intl"
import {
  Archive,
  ArchiveRestore,
  FileCode2,
  LayoutDashboard,
  Loader2,
  RefreshCw,
  ShieldCheck,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import {
  Drawer,
  DrawerContent,
  DrawerDescription,
  DrawerHeader,
  DrawerTitle,
  SIDE_PANEL_CONTENT_CLASS,
} from "@/components/ui/drawer"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  createDashboardMutation,
  DashboardApiError,
  getDashboard,
  getDashboardAccess,
  getDashboardIdentity,
  listDashboardGrants,
} from "@/lib/dashboard-api"
import type {
  Dashboard,
  DashboardGrant,
  DashboardIdentity,
} from "@/lib/dashboard-types"
import { DashboardAccessPanel } from "./dashboard-access-panel"
import { DashboardContentPanel } from "./dashboard-content-panel"
import {
  DASHBOARD_TAB_ACTIVE,
  DashboardError,
  DashboardMutationNotice,
  DashboardStatusBadge,
  useDashboardDate,
} from "./dashboard-common"
import {
  DashboardConfirm,
  type DashboardConfirmation,
} from "./dashboard-confirm"
import type { DashboardMutationController } from "./use-dashboard-mutation"

export function DashboardManager({
  id,
  identity,
  origin,
  refreshKey,
  mutation,
  onClose,
  onIdentityChanged,
}: {
  id: string
  identity: DashboardIdentity
  origin: string
  refreshKey: number
  mutation: DashboardMutationController
  onClose: () => void
  onIdentityChanged: () => void
}) {
  const t = useTranslations("Dashboards")
  const date = useDashboardDate()
  const [dashboard, setDashboard] = useState<Dashboard | null>(null)
  const [grants, setGrants] = useState<DashboardGrant[]>([])
  const [sources, setSources] = useState<DashboardGrant[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)
  const [accessError, setAccessError] = useState<unknown>(null)
  const [reload, setReload] = useState(0)
  const [tab, setTab] = useState("overview")
  const [confirmation, setConfirmation] =
    useState<DashboardConfirmation | null>(null)
  const generation = useRef(0)
  const identityChanged = useRef(onIdentityChanged)
  useEffect(() => {
    identityChanged.current = onIdentityChanged
  }, [onIdentityChanged])

  useEffect(() => {
    const ticket = ++generation.current
    setLoading(true)
    setError(null)
    setAccessError(null)
    setConfirmation(null)
    async function load() {
      try {
        const [me, board] = await Promise.all([
          getDashboardIdentity(),
          getDashboard(id),
        ])
        if (ticket !== generation.current) return
        if (me.principal_id !== identity.principal_id)
          throw new DashboardApiError("dashboard_identity_changed", 401)
        setDashboard(board)
        const owner = board.role === "owner" && me.scopes.includes("manage")
        const accessResults = await Promise.allSettled([
          owner
            ? listDashboardGrants(id)
            : Promise.resolve({
                items: [] as DashboardGrant[],
                revision: board.revision,
              }),
          getDashboardAccess(id),
        ])
        if (ticket !== generation.current) return
        const [grantResult, sourceResult] = accessResults
        if (grantResult.status === "fulfilled")
          setGrants(grantResult.value.items)
        else {
          setGrants([])
          setAccessError(grantResult.reason)
        }
        if (sourceResult.status === "fulfilled")
          setSources(sourceResult.value.sources)
        else {
          setSources([])
          setAccessError(sourceResult.reason)
        }
      } catch (cause) {
        if (ticket !== generation.current) return
        if (cause instanceof DashboardApiError && cause.status === 401) {
          identityChanged.current()
          return
        }
        setDashboard(null)
        setError(cause)
      } finally {
        if (ticket === generation.current) setLoading(false)
      }
    }
    void load()
    return () => {
      // Request generation, not a DOM ref: invalidate every late response.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      ++generation.current
    }
  }, [id, identity.principal_id, refreshKey, reload])

  const canEdit =
    !!dashboard &&
    ["owner", "editor"].includes(dashboard.role ?? "") &&
    identity.scopes.includes("write")
  const canManage =
    dashboard?.role === "owner" && identity.scopes.includes("manage")
  const canPublish =
    canEdit &&
    dashboard?.status !== "archived" &&
    (dashboard?.status === "published" || dashboard?.role === "owner")
  const accessSummary =
    canManage && !accessError
      ? t("accessSummary", {
          count: grants.filter(
            (grant) => grant.subject_type !== "all_authenticated"
          ).length,
          public: grants.some(
            (grant) => grant.subject_type === "all_authenticated"
          )
            ? t("enabled")
            : t("disabled"),
        })
      : t("existingAccessPreserved")

  return (
    <Drawer
      open
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
      swipeDirection="right"
    >
      <DrawerContent className={SIDE_PANEL_CONTENT_CLASS}>
        <DrawerHeader className="shrink-0 gap-0 border-b px-5 py-4">
          <div className="flex items-start gap-3 pr-8">
            <div className="flex size-9 shrink-0 items-center justify-center rounded-xl border bg-muted/40">
              <LayoutDashboard className="size-4" />
            </div>
            <div className="min-w-0 flex-1 space-y-2">
              <DrawerTitle className="break-words text-[0.9375rem] font-semibold leading-5">
                {dashboard?.title ?? t("manage")}
              </DrawerTitle>
              <DrawerDescription className="text-xs">
                {t("manageHere")}
              </DrawerDescription>
              {dashboard && (
                <div className="flex flex-wrap items-center gap-2">
                  <DashboardStatusBadge status={dashboard.status} />
                  <span className="text-[0.6875rem] text-muted-foreground">
                    {dashboard.owner_name} · {t(dashboard.role ?? "noAccess")}
                  </span>
                </div>
              )}
            </div>
          </div>
        </DrawerHeader>
        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          <div className="mb-4">
            <DashboardMutationNotice mutation={mutation} />
          </div>
          <DashboardError error={error} />
          {loading && (
            <p
              role="status"
              className="flex items-center gap-2 py-4 text-xs text-muted-foreground"
            >
              <Loader2 className="size-4 animate-spin" />
              {t("loading")}
            </p>
          )}
          {!!error && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => setReload((value) => value + 1)}
            >
              {t("refresh")}
            </Button>
          )}
          {dashboard && !loading && (
            <Tabs value={tab} onValueChange={setTab} className="space-y-4">
              <TabsList className="w-full">
                <TabsTrigger value="overview" className={DASHBOARD_TAB_ACTIVE}>
                  {t("overview")}
                </TabsTrigger>
                <TabsTrigger value="content" className={DASHBOARD_TAB_ACTIVE}>
                  <FileCode2 className="size-3.5" />
                  {t("content")}
                </TabsTrigger>
                <TabsTrigger value="access" className={DASHBOARD_TAB_ACTIVE}>
                  <ShieldCheck className="size-3.5" />
                  {t("accessTab")}
                </TabsTrigger>
              </TabsList>
              <TabsContent
                value="overview"
                className="space-y-5"
                key={`overview-${dashboard.id}-${dashboard.revision}`}
              >
                <form
                  className="space-y-3"
                  onSubmit={(event) => {
                    event.preventDefault()
                    const data = new FormData(event.currentTarget)
                    void mutation.perform(() =>
                      createDashboardMutation(
                        identity,
                        `/dashboards/${encodeURIComponent(dashboard.id)}`,
                        "PATCH",
                        {
                          title: String(data.get("title")).trim(),
                          description: String(data.get("description")),
                          expected_revision: dashboard.revision,
                        }
                      )
                    )
                  }}
                >
                  <fieldset
                    disabled={!canEdit || mutation.blocked}
                    className="space-y-3"
                  >
                    <label className="block space-y-1.5 text-xs font-medium">
                      {t("titleLabel")}
                      <Input
                        name="title"
                        defaultValue={dashboard.title}
                        required
                        maxLength={200}
                      />
                    </label>
                    <label className="block space-y-1.5 text-xs font-medium">
                      {t("descriptionLabel")}
                      <Textarea
                        name="description"
                        defaultValue={dashboard.description}
                        maxLength={2000}
                        rows={4}
                        className="resize-y text-sm"
                      />
                    </label>
                    {canEdit && (
                      <Button type="submit" size="sm">
                        {t("saveMetadata")}
                      </Button>
                    )}
                  </fieldset>
                </form>
                <div className="space-y-2 rounded-xl border p-4 text-xs">
                  <div className="flex justify-between gap-3">
                    <span className="text-muted-foreground">
                      {t("updatedAt")}
                    </span>
                    <span>{date(dashboard.updated_at)}</span>
                  </div>
                  <div className="flex justify-between gap-3">
                    <span className="text-muted-foreground">
                      {t("publishedAt")}
                    </span>
                    <span>{date(dashboard.published_at)}</span>
                  </div>
                  <div className="flex justify-between gap-3">
                    <span className="text-muted-foreground">
                      {t("liveVersion")}
                    </span>
                    <span>
                      {dashboard.current_version_number
                        ? `v${dashboard.current_version_number}`
                        : "—"}
                    </span>
                  </div>
                  {canEdit && (
                    <div className="flex justify-between gap-3">
                      <span className="text-muted-foreground">
                        {t("draftVersion")}
                      </span>
                      <span>
                        {dashboard.draft_version_number
                          ? `v${dashboard.draft_version_number}`
                          : t("noDraft")}
                      </span>
                    </div>
                  )}
                </div>
                {canEdit &&
                  !dashboard.current_version_id &&
                  !dashboard.draft_version_id && (
                    <div className="space-y-2 rounded-xl border border-dashed p-4">
                      <p className="text-xs leading-5 text-muted-foreground">
                        {t("emptyDraftHelp")}
                      </p>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setTab("content")}
                      >
                        {t("addContent")}
                      </Button>
                    </div>
                  )}
                {canManage && (
                  <section
                    className="space-y-3 rounded-2xl border p-4"
                    aria-label={t("status")}
                  >
                    <h3 className="text-sm font-medium">{t("status")}</h3>
                    <p className="text-xs leading-5 text-muted-foreground">
                      {dashboard.status === "archived"
                        ? t("restoreImpact")
                        : t("archiveImpact")}
                    </p>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={mutation.blocked}
                      onClick={() =>
                        setConfirmation({
                          title: t(
                            dashboard.status === "archived"
                              ? "restoreDraft"
                              : "archive"
                          ),
                          description: t(
                            dashboard.status === "archived"
                              ? "restoreImpact"
                              : "archiveImpact"
                          ),
                          action: () => {
                            void mutation.perform(() =>
                              createDashboardMutation(
                                identity,
                                `/dashboards/${encodeURIComponent(dashboard.id)}/${dashboard.status === "archived" ? "restore" : "archive"}`,
                                "POST",
                                { expected_revision: dashboard.revision }
                              )
                            )
                          },
                        })
                      }
                    >
                      {dashboard.status === "archived" ? (
                        <ArchiveRestore className="size-3.5" />
                      ) : (
                        <Archive className="size-3.5" />
                      )}
                      {t(
                        dashboard.status === "archived"
                          ? "restoreDraft"
                          : "archive"
                      )}
                    </Button>
                  </section>
                )}
              </TabsContent>
              <TabsContent value="content">
                <DashboardContentPanel
                  key={`${dashboard.id}-${dashboard.revision}`}
                  dashboard={dashboard}
                  identity={identity}
                  origin={origin}
                  mutation={mutation}
                  canEdit={canEdit}
                  canPublish={canPublish}
                  accessSummary={accessSummary}
                />
              </TabsContent>
              <TabsContent value="access">
                <DashboardError error={accessError} />
                {canManage && !accessError ? (
                  <DashboardAccessPanel
                    key={`${dashboard.id}-${dashboard.revision}`}
                    dashboard={dashboard}
                    identity={identity}
                    grants={grants}
                    mutation={mutation}
                  />
                ) : (
                  !canManage && (
                    <div className="space-y-3">
                      <p className="text-xs leading-5 text-muted-foreground">
                        {t("accessReadOnly")}
                      </p>
                      {sources.map((source, index) => (
                        <div
                          key={index}
                          className="rounded-xl border p-3 text-xs"
                        >
                          <p>
                            {source.subject_type === "all_authenticated"
                              ? t("publicAccess")
                              : t("effectiveAccess")}{" "}
                            · {t(source.role)}
                          </p>
                          <p className="mt-1 text-muted-foreground">
                            {source.expires_at
                              ? date(source.expires_at)
                              : t("forever")}
                          </p>
                        </div>
                      ))}
                    </div>
                  )
                )}
              </TabsContent>
            </Tabs>
          )}
        </div>
        <div className="flex shrink-0 items-center justify-between gap-3 border-t px-5 py-3">
          <p className="truncate text-[0.625rem] text-muted-foreground">
            {t("idLabel")}: {id}
          </p>
          <Button
            size="sm"
            variant="ghost"
            disabled={loading || mutation.busy}
            onClick={() => setReload((value) => value + 1)}
          >
            <RefreshCw className="size-3.5" />
            {t("refresh")}
          </Button>
        </div>
        <DashboardConfirm
          confirmation={confirmation}
          onClose={() => setConfirmation(null)}
        />
      </DrawerContent>
    </Drawer>
  )
}
