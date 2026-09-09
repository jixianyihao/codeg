"use client"

import { useEffect, useRef, useSyncExternalStore } from "react"
import {
  DashboardApiError,
  executeDashboardMutation,
  getDashboardOperation,
} from "@/lib/dashboard-api"
import type {
  DashboardMutation,
  DashboardOperation,
} from "@/lib/dashboard-types"

interface MutationState {
  pending: DashboardMutation | null
  busy: boolean
  error: unknown
  notice: "saved" | "pending" | null
  completed: DashboardOperation | null
}
const emptyState: MutationState = {
  pending: null,
  busy: false,
  error: null,
  notice: null,
  completed: null,
}
// One bounded, volatile operation for this browser window. Workbench routes
// unmount their pages: keeping bytes here preserves the original retry across
// route changes without persisting either HTML or credentials to web storage.
let state = emptyState
let generation = 0
let authListenersInstalled = false
const listeners = new Set<() => void>()
function update(values: Partial<MutationState>) {
  state = { ...state, ...values }
  listeners.forEach((listener) => listener())
}
function clear() {
  ++generation
  state = emptyState
  listeners.forEach((listener) => listener())
}
function dismissFeedback() {
  // Starting a different task dismisses completed feedback only. A pending
  // request remains recoverable even when a different dashboard is opened.
  if (state.busy || state.pending) return
  update({ error: null, notice: null, completed: null })
}
function installAuthListeners() {
  if (authListenersInstalled || typeof window === "undefined") return
  authListenersInstalled = true
  window.addEventListener("aresclaw:auth-changed", clear)
  window.addEventListener("storage", (event) => {
    if (!event.key || event.key === "codeg_token") clear()
  })
}
function subscribe(listener: () => void) {
  installAuthListeners()
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
const snapshot = () => state
const serverSnapshot = () => emptyState

async function perform(
  create: () => DashboardMutation | Promise<DashboardMutation>,
  mode: "new" | "retry" | "query" = "new"
) {
  if (state.busy || (state.pending && mode === "new")) return false
  update({ busy: true, error: null, notice: null })
  const ticket = generation
  let request: DashboardMutation | null = null
  try {
    request = await create()
    if (ticket !== generation) return false
    update({ pending: request })
    const outcome = await (mode === "query"
      ? getDashboardOperation(request)
      : executeDashboardMutation(request))
    if (ticket !== generation) return false
    if (outcome.state === "succeeded") {
      update({ pending: null, notice: "saved", completed: outcome })
      return true
    }
    if (outcome.state === "failed") {
      update({
        // An interrupted upload is terminal for its attempt, but the service
        // explicitly allows the SAME operation to resume. Keep its bytes.
        pending: outcome.error?.retryable ? request : null,
        error: new DashboardApiError(
          outcome.error?.code || "operation_failed",
          409
        ),
        notice: outcome.error?.retryable ? "pending" : null,
      })
    } else update({ notice: "pending" })
  } catch (cause) {
    if (ticket !== generation) return false
    const failure =
      cause instanceof DashboardApiError
        ? cause
        : new DashboardApiError("network_outcome_unknown")
    if (
      failure.status === 401 ||
      failure.code === "dashboard_identity_changed"
    ) {
      clear()
      update({ error: failure })
      return false
    }
    // A missing operation is not proof of failure: the original request may
    // still be arriving. A retry must retain the same key and frozen bytes.
    if (
      !request ||
      (!failure.uncertain && !(mode === "query" && failure.status === 404))
    )
      update({ pending: null })
    update({ error: failure })
  } finally {
    if (ticket === generation) update({ busy: false })
  }
  return false
}
const retry = () =>
  state.pending
    ? perform(() => state.pending!, "retry")
    : Promise.resolve(false)
const query = () =>
  state.pending
    ? perform(() => state.pending!, "query")
    : Promise.resolve(false)

export function reconcileDashboardMutationIdentity(principalId: string) {
  if (
    state.pending &&
    (state.pending.principal_id !== principalId ||
      state.pending.service_origin !== window.location.origin)
  )
    clear()
}

export function useDashboardMutation(
  onSuccess: (operation: DashboardOperation) => void,
  onIdentityChanged: () => void
) {
  const current = useSyncExternalStore(subscribe, snapshot, serverSnapshot)
  const lastCompleted = useRef(current.completed)
  const callbacks = useRef({ onSuccess, onIdentityChanged })
  useEffect(() => {
    callbacks.current = { onSuccess, onIdentityChanged }
  }, [onSuccess, onIdentityChanged])
  useEffect(() => {
    if (current.completed && current.completed !== lastCompleted.current) {
      lastCompleted.current = current.completed
      callbacks.current.onSuccess(current.completed)
    }
  }, [current.completed])
  useEffect(() => {
    if (
      current.error instanceof DashboardApiError &&
      current.error.status === 401
    )
      callbacks.current.onIdentityChanged()
  }, [current.error])
  return {
    ...current,
    perform,
    retry,
    query,
    clear,
    dismissFeedback,
    blocked: current.busy || !!current.pending,
  }
}
export type DashboardMutationController = ReturnType<
  typeof useDashboardMutation
>
