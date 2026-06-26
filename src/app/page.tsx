"use client"

import { useEffect } from "react"
import { useRouter } from "next/navigation"
import { isDesktop } from "@/lib/platform"
import {
  replaceWithServerBasePath,
  withServerBasePath,
} from "@/lib/server-base-path"

export default function Page() {
  const router = useRouter()
  useEffect(() => {
    if (isDesktop()) {
      router.replace("/workspace")
      return
    }
    // Web mode: validate token before entering app
    const token = localStorage.getItem("codeg_token")
    if (!token) {
      replaceWithServerBasePath(router, "/login")
      return
    }
    // Verify token is still valid
    fetch(withServerBasePath("/api/health"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: "{}",
    })
      .then((res) => {
        if (res.ok) {
          replaceWithServerBasePath(router, "/workspace")
          return
        }
        if (res.status === 401) {
          // Token genuinely rejected → clear it and re-authenticate.
          localStorage.removeItem("codeg_token")
          replaceWithServerBasePath(router, "/login")
          return
        }
        // Server reachable but unhealthy (5xx / proxy error). Keep the token
        // and enter the app; the in-app reconnect dialog handles recovery
        // instead of bouncing a valid session to /login.
        replaceWithServerBasePath(router, "/workspace")
      })
      .catch(() => {
        // Server unreachable (restart, network blip, sleep/wake). The token is
        // almost certainly still valid — don't discard it. Enter the workspace
        // and let WebConnectionGuard surface the offline state and recover.
        replaceWithServerBasePath(router, "/workspace")
      })
  }, [router])
  return null
}
