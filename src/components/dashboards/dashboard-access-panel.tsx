"use client"

import { useEffect, useRef, useState } from "react"
import { useTranslations } from "next-intl"
import {
  Globe2,
  Pencil,
  Search,
  ShieldCheck,
  Trash2,
  Users,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  createDashboardMutation,
  searchDashboardPrincipals,
} from "@/lib/dashboard-api"
import type {
  Dashboard,
  DashboardGrant,
  DashboardIdentity,
  DashboardPrincipal,
  DashboardSubjectType,
} from "@/lib/dashboard-types"
import {
  DashboardError,
  dashboardTimeWindow,
  localDateInput,
  useDashboardDate,
} from "./dashboard-common"
import {
  DashboardConfirm,
  type DashboardConfirmation,
} from "./dashboard-confirm"
import type { DashboardMutationController } from "./use-dashboard-mutation"

export function DashboardAccessPanel({
  dashboard,
  identity,
  grants,
  mutation,
}: {
  dashboard: Dashboard
  identity: DashboardIdentity
  grants: DashboardGrant[]
  mutation: DashboardMutationController
}) {
  const t = useTranslations("Dashboards")
  const date = useDashboardDate()
  const publicGrant = grants.find(
    (grant) => grant.subject_type === "all_authenticated"
  )
  const [subjectType, setSubjectType] = useState<DashboardSubjectType>("user")
  const [query, setQuery] = useState("")
  const [results, setResults] = useState<DashboardPrincipal[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [selected, setSelected] = useState<DashboardPrincipal | null>(null)
  const [role, setRole] = useState<"viewer" | "editor">("viewer")
  const [start, setStart] = useState("")
  const [end, setEnd] = useState("")
  const [publicEnabled, setPublicEnabled] = useState(!!publicGrant)
  const [publicStart, setPublicStart] = useState(
    localDateInput(publicGrant?.starts_at ?? null)
  )
  const [publicEnd, setPublicEnd] = useState(
    localDateInput(publicGrant?.expires_at ?? null)
  )
  const [searching, setSearching] = useState(false)
  const [searched, setSearched] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [confirmation, setConfirmation] =
    useState<DashboardConfirmation | null>(null)
  const generation = useRef(0)
  const form = useRef<HTMLFormElement>(null)
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone
  const base = `/dashboards/${encodeURIComponent(dashboard.id)}`
  useEffect(
    () => () => {
      ++generation.current
    },
    []
  )

  function resetSearch() {
    ++generation.current
    setSelected(null)
    setResults([])
    setCursor(null)
    setSearched(false)
    setSearching(false)
  }
  async function search(append = false) {
    if (!query.trim()) return
    const ticket = ++generation.current
    setSearching(true)
    setError(null)
    try {
      const page = await searchDashboardPrincipals(
        subjectType,
        query.trim(),
        append ? (cursor ?? undefined) : undefined
      )
      if (ticket !== generation.current) return
      setResults((previous) =>
        append
          ? [
              ...previous,
              ...page.items.filter(
                (item) => !previous.some((old) => old.id === item.id)
              ),
            ]
          : page.items
      )
      setCursor(page.next_cursor)
      setSearched(true)
    } catch (cause) {
      if (ticket === generation.current) setError(cause)
    } finally {
      if (ticket === generation.current) setSearching(false)
    }
  }
  function editGrant(grant: DashboardGrant) {
    if (grant.subject_type === "all_authenticated") return
    resetSearch()
    setSubjectType(grant.subject_type)
    setSelected({
      id: grant.subject_id,
      type: grant.subject_type,
      display_name: grant.subject_name || grant.subject_id,
    })
    setQuery("")
    setRole(grant.role)
    setStart(localDateInput(grant.starts_at))
    setEnd(localDateInput(grant.expires_at))
    form.current?.scrollIntoView?.({ behavior: "smooth", block: "nearest" })
  }
  const typeLabel = (type: DashboardGrant["subject_type"]) =>
    t(
      type === "all_authenticated"
        ? "publicAccess"
        : type === "user"
          ? "person"
          : type === "group"
            ? "group"
            : "service"
    )
  function timeFields(
    prefix: string,
    from: string,
    until: string,
    setFrom: (value: string) => void,
    setUntil: (value: string) => void
  ) {
    return (
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block space-y-1.5 text-xs">
          {t("startsAt")}
          <Input
            aria-label={`${prefix} ${t("startsAt")}`}
            type="datetime-local"
            value={from}
            onChange={(event) => setFrom(event.target.value)}
            className="min-w-0 text-xs"
          />
        </label>
        <label className="block space-y-1.5 text-xs">
          {t("endsAt")}
          <Input
            aria-label={`${prefix} ${t("endsAt")}`}
            type="datetime-local"
            value={until}
            onChange={(event) => setUntil(event.target.value)}
            className="min-w-0 text-xs"
          />
        </label>
      </div>
    )
  }

  return (
    <div className="space-y-5">
      <div className="flex items-start gap-2 rounded-xl bg-muted/40 p-3 text-xs leading-5 text-muted-foreground">
        <ShieldCheck className="mt-0.5 size-4 shrink-0" />
        <p>{t("accessHelp")}</p>
      </div>
      <DashboardError error={error} />
      <form
        ref={form}
        onSubmit={(event) => {
          event.preventDefault()
          if (!selected) return
          try {
            const window = dashboardTimeWindow(start, end)
            void mutation.perform(() =>
              createDashboardMutation(identity, `${base}/grants`, "POST", {
                subject_type: selected.type,
                subject_id: selected.id,
                role,
                ...window,
                expected_revision: dashboard.revision,
              })
            )
          } catch (cause) {
            setError(cause)
          }
        }}
      >
        <fieldset
          disabled={mutation.blocked}
          className="space-y-3 rounded-2xl border p-4"
        >
          <legend className="flex items-center gap-2 px-1 text-sm font-medium">
            <Users className="size-4 text-muted-foreground" />
            {t("grantAccess")}
          </legend>
          <div className="flex gap-2">
            <Select
              value={subjectType}
              onValueChange={(value) => {
                setSubjectType(value as DashboardSubjectType)
                resetSearch()
              }}
              disabled={mutation.blocked}
            >
              <SelectTrigger
                aria-label={t("subjectType")}
                size="sm"
                className="shrink-0"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(["user", "group", "service"] as const).map((type) => (
                  <SelectItem key={type} value={type}>
                    {typeLabel(type)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Input
              aria-label={t("principalSearch")}
              placeholder={t("principalSearch")}
              value={query}
              onChange={(event) => {
                setQuery(event.target.value)
                resetSearch()
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault()
                  void search()
                }
              }}
              className="h-8 min-w-0 text-xs"
            />
            <Button
              type="button"
              size="icon-sm"
              variant="outline"
              aria-label={t("principalSearch")}
              disabled={searching || !query.trim()}
              onClick={() => void search()}
            >
              <Search className="size-3.5" />
            </Button>
          </div>
          {results.length > 0 && (
            <div
              className="max-h-44 space-y-1 overflow-auto rounded-xl border p-1"
              aria-label={t("searchResults")}
            >
              {results.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className="block w-full rounded-lg px-2.5 py-2 text-left hover:bg-muted focus-visible:outline-primary disabled:opacity-50"
                  onClick={() => {
                    setSelected(item)
                    setResults([])
                    setSearched(false)
                  }}
                >
                  <span className="block text-xs font-medium">
                    {item.display_name}
                  </span>
                  <span className="block truncate font-mono text-[0.625rem] text-muted-foreground">
                    {item.id}
                  </span>
                </button>
              ))}
            </div>
          )}
          {searched && results.length === 0 && !selected && (
            <p className="text-xs text-muted-foreground">{t("noPrincipals")}</p>
          )}
          {cursor && (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={searching}
              onClick={() => void search(true)}
            >
              {t("more")}
            </Button>
          )}
          {selected && (
            <div className="rounded-xl bg-primary/5 p-3 text-xs">
              <p className="font-medium">
                {typeLabel(selected.type)} · {selected.display_name}
              </p>
              <p className="mt-1 break-all font-mono text-[0.625rem] text-muted-foreground">
                {selected.id}
              </p>
            </div>
          )}
          <label className="flex items-center justify-between gap-3 text-xs">
            {t("roleLabel")}
            <Select
              value={role}
              onValueChange={(value) => setRole(value as "viewer" | "editor")}
              disabled={mutation.blocked}
            >
              <SelectTrigger size="sm" aria-label={t("roleLabel")}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="viewer">{t("viewer")}</SelectItem>
                <SelectItem value="editor">{t("editor")}</SelectItem>
              </SelectContent>
            </Select>
          </label>
          {timeFields(t("grantAccess"), start, end, setStart, setEnd)}
          <p className="text-[0.6875rem] leading-5 text-muted-foreground">
            {t("dateHelp", { timezone })}
          </p>
          <Button
            type="submit"
            size="sm"
            disabled={!selected || mutation.blocked}
          >
            {t("saveGrant")}
          </Button>
        </fieldset>
      </form>
      <section className="space-y-3" aria-label={t("existingGrants")}>
        <h3 className="text-sm font-medium">{t("existingGrants")}</h3>
        {grants.filter((grant) => grant.subject_type !== "all_authenticated")
          .length === 0 && (
          <p className="rounded-xl border border-dashed p-4 text-xs text-muted-foreground">
            {t("noGrants")}
          </p>
        )}
        {grants
          .filter((grant) => grant.subject_type !== "all_authenticated")
          .map((grant) => (
            <div
              key={`${grant.subject_type}:${grant.subject_id}`}
              className="space-y-2 rounded-xl border p-3"
            >
              <div className="flex items-center justify-between gap-2">
                <p className="text-xs font-medium">
                  {grant.subject_name || typeLabel(grant.subject_type)} ·{" "}
                  {t(grant.role)}
                </p>
                <div className="flex gap-1">
                  <Button
                    size="icon-sm"
                    variant="ghost"
                    aria-label={t("editGrant", { id: grant.subject_id })}
                    disabled={mutation.blocked}
                    onClick={() => editGrant(grant)}
                  >
                    <Pencil className="size-3.5" />
                  </Button>
                  <Button
                    size="icon-sm"
                    variant="ghost"
                    aria-label={t("revokeGrant", { id: grant.subject_id })}
                    disabled={mutation.blocked}
                    onClick={() =>
                      setConfirmation({
                        title: t("revoke"),
                        description: t("revokeImpact", {
                          id: grant.subject_id,
                        }),
                        action: () => {
                          void mutation.perform(() =>
                            createDashboardMutation(
                              identity,
                              `${base}/grants/${grant.subject_type}/${encodeURIComponent(grant.subject_id)}?expected_revision=${dashboard.revision}`,
                              "DELETE"
                            )
                          )
                        },
                      })
                    }
                  >
                    <Trash2 className="size-3.5" />
                  </Button>
                </div>
              </div>
              <p className="break-all font-mono text-[0.625rem] text-muted-foreground">
                {grant.subject_id}
              </p>
              <p className="text-[0.6875rem] leading-5 text-muted-foreground">
                {grant.starts_at ? date(grant.starts_at) : t("immediately")} →{" "}
                {grant.expires_at ? date(grant.expires_at) : t("forever")}
                {grant.expires_at && Date.parse(grant.expires_at) <= Date.now()
                  ? ` · ${t("expired")}`
                  : ""}
              </p>
            </div>
          ))}
      </section>
      <form
        onSubmit={(event) => {
          event.preventDefault()
          try {
            const payload = {
              enabled: publicEnabled,
              ...(publicEnabled
                ? dashboardTimeWindow(publicStart, publicEnd)
                : {}),
              expected_revision: dashboard.revision,
            }
            setConfirmation({
              title: t("savePublic"),
              description: publicEnabled
                ? t("publicEnableImpact")
                : t("publicDisableImpact"),
              action: () => {
                void mutation.perform(() =>
                  createDashboardMutation(
                    identity,
                    `${base}/public-access`,
                    "PUT",
                    payload
                  )
                )
              },
            })
          } catch (cause) {
            setError(cause)
          }
        }}
      >
        <fieldset
          disabled={mutation.blocked}
          className="space-y-3 rounded-2xl border p-4"
        >
          <legend className="flex items-center gap-2 px-1 text-sm font-medium">
            <Globe2 className="size-4 text-muted-foreground" />
            {t("publicAccess")}
          </legend>
          <label className="flex items-start gap-2.5 text-xs leading-5">
            <Checkbox
              checked={publicEnabled}
              disabled={mutation.blocked}
              onCheckedChange={(value) => setPublicEnabled(value === true)}
              className="mt-0.5"
            />
            <span>{t("publicLabel")}</span>
          </label>
          {publicEnabled &&
            timeFields(
              t("publicAccess"),
              publicStart,
              publicEnd,
              setPublicStart,
              setPublicEnd
            )}
          <p className="text-[0.6875rem] leading-5 text-muted-foreground">
            {t("publicHelp")}
          </p>
          <Button type="submit" size="sm" variant="outline">
            {t("savePublic")}
          </Button>
        </fieldset>
      </form>
      <DashboardConfirm
        confirmation={confirmation}
        onClose={() => setConfirmation(null)}
      />
    </div>
  )
}
