"use client"

import { useEffect } from "react"
import { useRouter } from "next/navigation"
import { replaceWithServerBasePath } from "@/lib/server-base-path"

export default function SettingsPage() {
  const router = useRouter()

  useEffect(() => {
    replaceWithServerBasePath(router, "/settings/appearance")
  }, [router])

  return null
}
