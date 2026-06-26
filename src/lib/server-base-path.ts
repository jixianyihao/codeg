declare global {
  interface Window {
    __CODEG_BASE_PATH__?: string
  }
}

function normalizeBasePath(value: string | undefined): string {
  const trimmed = value?.trim()
  if (!trimmed || trimmed === "/") return ""
  const withLeadingSlash = trimmed.startsWith("/") ? trimmed : `/${trimmed}`
  return withLeadingSlash.replace(/\/+$/, "")
}

export function getServerBasePath(): string {
  if (typeof window === "undefined") return ""
  return normalizeBasePath(window.__CODEG_BASE_PATH__)
}

export function withServerBasePath(path: string): string {
  if (/^[a-z][a-z\d+.-]*:/i.test(path)) return path

  const basePath = getServerBasePath()
  const normalizedPath = path.startsWith("/") ? path : `/${path}`
  if (!basePath) return normalizedPath
  if (normalizedPath === "/") return basePath
  return `${basePath}${normalizedPath}`
}

export function stripServerBasePath(path: string): string {
  const basePath = getServerBasePath()
  if (!basePath) return path
  if (path === basePath) return "/"
  if (path.startsWith(`${basePath}/`)) return path.slice(basePath.length)
  return path
}

export function replaceWithServerBasePath(
  router: { replace(path: string): void },
  path: string
): void {
  const target = withServerBasePath(path)
  if (getServerBasePath()) {
    window.location.replace(target)
    return
  }
  router.replace(target)
}

export {}
