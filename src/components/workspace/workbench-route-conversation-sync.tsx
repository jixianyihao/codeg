"use client"

import { useEffect, useRef } from "react"
import { useTabActions, useTabStore } from "@/contexts/tab-context"
import { useWorkbenchRoute } from "@/contexts/workbench-route-context"

// Keep local conversation activation visible without letting a remote client's
// mirrored tab focus replace a full-page workbench route.
export function WorkbenchRouteConversationSync() {
  const activeTabId = useTabStore((s) => s.activeTabId)
  const { consumeRemoteActivation } = useTabActions()
  const { openConversations } = useWorkbenchRoute()
  const prevRef = useRef(activeTabId)
  const firstActivation = useRef(true)
  useEffect(() => {
    if (prevRef.current === activeTabId) return
    const previous = prevRef.current
    const initial = firstActivation.current
    firstActivation.current = false
    prevRef.current = activeTabId
    if (consumeRemoteActivation()) return
    // Hydrating saved tabs or recovering an empty workspace creates its first
    // tab in the background. Preserve the explicit dashboard entry for that
    // one transition. User openers still call openConversations directly, and
    // every subsequent local activation keeps the normal synchronization.
    if (
      initial &&
      previous === null &&
      activeTabId !== null &&
      new URLSearchParams(window.location.search).get("view") === "dashboards"
    )
      return
    openConversations()
  }, [activeTabId, openConversations, consumeRemoteActivation])
  return null
}
