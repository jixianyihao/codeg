import { beforeEach, describe, expect, it, vi } from "vitest"

const state = vi.hoisted(() => ({ call: vi.fn(), token: "user-a" }))
vi.mock("@/lib/transport", () => ({
  getTransport: () => ({ call: state.call }),
}))
vi.mock("@/lib/transport/web-auth", () => ({
  getCodegToken: () => state.token,
}))

import { listDashboards, dashboardViewUrl } from "./dashboard-api"

describe("dashboard metadata client", () => {
  beforeEach(() => {
    state.token = "user-a"
    state.call.mockReset()
  })

  it("rejects a late response after switching the signed-in identity", async () => {
    let resolve!: (value: unknown) => void
    state.call.mockReturnValue(
      new Promise((done) => {
        resolve = done
      })
    )
    const pending = listDashboards({ scope: "mine" })
    state.token = "user-b"
    resolve({
      items: [],
      next_cursor: null,
      service_origin: "https://boards.internal",
      principal_id: "a",
    })
    await expect(pending).rejects.toThrow("dashboard_identity_changed")
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
  })

  it("preserves service failures instead of returning a successful empty page", async () => {
    state.call.mockRejectedValue(new Error("dashboard_unavailable"))
    await expect(listDashboards({ scope: "shared" })).rejects.toThrow(
      "dashboard_unavailable"
    )
  })
})
