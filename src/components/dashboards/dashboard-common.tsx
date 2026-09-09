"use client"

import { useLocale, useTranslations } from "next-intl"
import { Check, FilePenLine, Archive, Loader2 } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { DashboardApiError } from "@/lib/dashboard-api"
import type { DashboardStatus } from "@/lib/dashboard-types"
import type { DashboardMutationController } from "./use-dashboard-mutation"

export const DASHBOARD_TAB_ACTIVE =
  "data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow-sm"

export function DashboardStatusBadge({ status }: { status: DashboardStatus }) {
  const t = useTranslations("Dashboards")
  const Icon =
    status === "published" ? Check : status === "draft" ? FilePenLine : Archive
  const tone =
    status === "published"
      ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
      : status === "draft"
        ? "bg-amber-500/10 text-amber-700 dark:text-amber-400"
        : "bg-muted text-muted-foreground"
  return (
    <Badge
      variant="secondary"
      className={`gap-1 border-0 px-2 py-1 text-[0.625rem] ${tone}`}
    >
      <Icon className="size-3" />
      {t(status)}
    </Badge>
  )
}

export function useDashboardDate() {
  const locale = useLocale()
  const t = useTranslations("Dashboards")
  return (value: string | null | undefined) => {
    if (!value) return t("notPublished")
    const parsed = new Date(value)
    return Number.isNaN(parsed.getTime())
      ? "—"
      : new Intl.DateTimeFormat(locale, {
          dateStyle: "medium",
          timeStyle: "short",
        }).format(parsed)
  }
}

export function DashboardError({ error }: { error: unknown }) {
  const t = useTranslations("Dashboards")
  if (!error) return null
  const failure = error instanceof DashboardApiError ? error : null
  const key =
    failure?.status === 401
      ? "authExpired"
      : failure?.status === 403 || failure?.status === 404
        ? "accessDenied"
        : failure?.code === "revision_conflict"
          ? "conflict"
          : failure?.code === "invalid_html_file" || failure?.status === 413
            ? "fileInvalid"
            : failure?.status === 507
              ? "quotaExceeded"
              : failure?.code === "invalid_time"
                ? "invalidDates"
                : failure?.uncertain
                  ? "unknownWrite"
                  : "error"
  return (
    <p
      role="alert"
      className="rounded-xl border border-destructive/20 bg-destructive/5 px-3 py-2 text-xs leading-5 text-destructive"
    >
      {t(key)}
      {failure?.traceId && (
        <span className="mt-1 block font-mono">
          {t("trace")}: {failure.traceId}
        </span>
      )}
    </p>
  )
}

export function DashboardMutationNotice({
  mutation,
}: {
  mutation: DashboardMutationController
}) {
  const t = useTranslations("Dashboards")
  return (
    <div className="space-y-2">
      <DashboardError error={mutation.error} />
      {mutation.notice === "saved" && (
        <p
          role="status"
          className="text-xs text-emerald-700 dark:text-emerald-400"
        >
          {t("saved")}
        </p>
      )}
      {mutation.pending && (
        <div className="space-y-2 rounded-xl border border-amber-500/25 bg-amber-500/5 p-3 text-xs">
          <p role="status" className="leading-5">
            {t("pendingWrite")}
          </p>
          <p className="break-all font-mono text-[0.625rem] text-muted-foreground">
            {mutation.pending.id}
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={mutation.busy}
              onClick={() => void mutation.query()}
            >
              {mutation.busy && <Loader2 className="size-3 animate-spin" />}
              {t("queryOperation")}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={mutation.busy}
              onClick={() => void mutation.retry()}
            >
              {t("retryOperation")}
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}

export function localDateInput(value: string | null) {
  if (!value) return ""
  const date = new Date(value)
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16)
}

export function dashboardTimeWindow(start: string, end: string) {
  const starts_at = start ? new Date(start).toISOString() : null
  const expires_at = end ? new Date(end).toISOString() : null
  if (starts_at && expires_at && starts_at >= expires_at)
    throw new DashboardApiError("invalid_time", 422)
  return { starts_at, expires_at }
}
