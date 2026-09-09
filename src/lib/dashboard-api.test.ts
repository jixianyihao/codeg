import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const state = vi.hoisted(() => ({ token: "user-a" }))
vi.mock("@/lib/transport/web-auth", () => ({
  getCodegToken: () => state.token,
}))

import { listDashboards, dashboardViewUrl } from "./dashboard-api"

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  })
}

describe("dashboard metadata client", () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    state.token = "user-a"
    fetchMock.mockReset()
    vi.stubGlobal("fetch", fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("calls the public GET API through the same-origin proxy with the web credential", async () => {
    fetchMock.mockImplementation((_input: RequestInfo | URL, init?: RequestInit) => {
      const headers = new Headers(init?.headers)
      expect(headers.get("Authorization")).toBe("Bearer user-a")
      const url = String(_input)
      if (url.endsWith("/me")) {
        return jsonResponse({
          principal_id: "p-1",
          principal_type: "human",
          display_name: "A",
        })
      }
      expect(url).toContain("/dashboards?scope=mine")
      return jsonResponse({
        items: [
          {
            id: "board-1",
            title: "Weekly",
            description: "",
            revision: 1,
            status: "published",
            role: "owner",
          },
        ],
        next_cursor: null,
      })
    })
    const page = await listDashboards({ scope: "mine" })
    expect(page.items.map((item) => item.id)).toEqual(["board-1"])
    expect(page.principal_id).toBe("p-1")
  })

  it("rejects a late response after switching the signed-in identity", async () => {
    let resolve!: (value: Response) => void
    fetchMock.mockReturnValue(
      new Promise((done) => {
        resolve = done
      })
    )
    const pending = listDashboards({ scope: "mine" })
    state.token = "user-b"
    resolve(jsonResponse({ principal_id: "a" }))
    await expect(pending).rejects.toThrow("dashboard_identity_changed")
  })

  it("filters malformed items and surfaces auth failures", async () => {
    fetchMock.mockImplementation((_input: RequestInfo | URL) => {
      const url = String(_input)
      if (url.endsWith("/me")) return jsonResponse({}, 401)
      return jsonResponse({ items: [{ title: "no id" }, { id: "ok", title: "T", revision: 2, status: "published" }], next_cursor: null })
    })
    await expect(listDashboards({ scope: "all" })).rejects.toThrow(
      "dashboard_unauthorized"
    )
  })

  it("returns only a configured origin's stable detail route", () => {
    expect(dashboardViewUrl("https://boards.internal", "board-123")).toBe(
      "https://boards.internal/dashboards/board-123"
    )
    expect(dashboardViewUrl("javascript:alert(1)", "board-123")).toBeNull()
    expect(
      dashboardViewUrl("https://user:secret@boards.internal", "board-123")
    ).toBeNull()
    expect(
      dashboardViewUrl("https://boards.internal", "../../admin")
    ).toBeNull()
    expect(dashboardViewUrl("http://boards.internal", "board-123")).toBeNull()
    expect(dashboardViewUrl("", "board-123")).toBeNull()
  })
})
