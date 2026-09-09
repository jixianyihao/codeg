import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { NextIntlClientProvider } from "next-intl"
import { beforeEach, expect, it, vi } from "vitest"
import messages from "@/i18n/messages/zh-CN.json"
import { DashboardApiError } from "@/lib/dashboard-api"
import type {
  Dashboard,
  DashboardIdentity,
  DashboardPage,
} from "@/lib/dashboard-types"
import { DashboardsPage } from "./dashboard-page"

const state = vi.hoisted(() => ({
  listDashboards: vi.fn(),
  getDashboard: vi.fn(),
  getDashboardIdentity: vi.fn(),
  listDashboardVersions: vi.fn(),
  listDashboardGrants: vi.fn(),
  getDashboardAccess: vi.fn(),
  executeDashboardMutation: vi.fn(),
  getDashboardOperation: vi.fn(),
  searchDashboardPrincipals: vi.fn(),
  openUrl: vi.fn(),
}))
vi.mock("@/lib/dashboard-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/dashboard-api")>()),
  ...state,
}))
vi.mock("@/lib/platform", () => ({
  isDesktop: () => false,
  openUrl: state.openUrl,
}))

const identity: DashboardIdentity = {
  principal_id: "user-a",
  principal_type: "human",
  display_name: "Alice",
  scopes: ["read", "write", "manage"],
}
const board: Dashboard = {
  id: "board-1",
  title: "发布周报 <img src=x>",
  description: "项目进展",
  owner_principal_id: "user-a",
  owner_name: "Alice",
  owner_type: "human",
  current_version_id: "v1",
  current_version_number: 1,
  draft_version_id: "v2",
  draft_version_number: 2,
  has_draft: true,
  revision: 1,
  status: "published",
  role: "owner",
  created_at: "2026-09-09T01:00:00Z",
  updated_at: "2026-09-09T01:00:00Z",
  published_at: "2026-09-09T01:00:00Z",
  expires_at: null,
}
const page: DashboardPage = {
  items: [board],
  next_cursor: null,
  principal_id: identity.principal_id,
  identity,
  service_origin: "https://boards.internal",
}
function mount() {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messages}>
      <DashboardsPage />
    </NextIntlClientProvider>
  )
}
beforeEach(() => {
  Object.values(state).forEach((mock) => mock.mockReset())
  localStorage.clear()
  localStorage.setItem("codeg_token", "token-a")
  fireEvent(window, new Event("aresclaw:auth-changed"))
  window.history.replaceState(null, "", "/workspace")
  state.listDashboards.mockResolvedValue(page)
  state.getDashboardIdentity.mockResolvedValue(identity)
  state.getDashboard.mockResolvedValue(board)
  state.listDashboardGrants.mockResolvedValue({ items: [], revision: 1 })
  state.getDashboardAccess.mockResolvedValue({ role: "owner", sources: [] })
  state.listDashboardVersions.mockResolvedValue({
    items: [
      {
        id: "v2",
        number: 2,
        byte_size: 200,
        created_at: board.updated_at,
        published_at: null,
      },
      {
        id: "v1",
        number: 1,
        byte_size: 100,
        created_at: board.updated_at,
        published_at: board.published_at,
      },
    ],
    next_cursor: null,
  })
  state.executeDashboardMutation.mockResolvedValue({
    state: "succeeded",
    operation_id: "op-1",
    result: { dashboard_id: board.id, status: "published", revision: 2 },
    error: null,
  })
  state.getDashboardOperation.mockResolvedValue({
    state: "processing",
    operation_id: "op-1",
    result: null,
    error: null,
  })
})

async function openManager() {
  const user = userEvent.setup()
  mount()
  await user.click(await screen.findByRole("button", { name: "管理看板" }))
  await screen.findByRole("button", { name: "保存信息" })
  return user
}

it("shows inert metadata cards and independent links", async () => {
  const { container } = mount()
  expect(await screen.findByText("发布周报 <img src=x>")).toBeInTheDocument()
  expect(screen.getByRole("link", { name: "打开看板" })).toHaveAttribute(
    "href",
    "https://boards.internal/dashboards/board-1"
  )
  expect(container.querySelector("iframe, img")).toBeNull()
  expect(screen.getByText("待发布更新 · v2")).toBeInTheDocument()
})
it("keeps discovered no-access cards inert", async () => {
  state.listDashboards.mockResolvedValue({
    ...page,
    items: [{ ...board, role: null }],
  })
  mount()
  await screen.findByText(board.title)
  expect(screen.queryByRole("link")).toBeNull()
  expect(screen.queryByRole("button", { name: "管理看板" })).toBeNull()
})
it("shows service failure and allows a successful retry", async () => {
  state.listDashboards
    .mockRejectedValueOnce(new Error("unavailable"))
    .mockResolvedValue(page)
  mount()
  expect(await screen.findByRole("alert")).toHaveTextContent("无法加载看板")
  fireEvent.click(screen.getByRole("button", { name: "刷新" }))
  expect(await screen.findByText("项目进展")).toBeInTheDocument()
})
it("distinguishes an empty successful listing", async () => {
  state.listDashboards.mockResolvedValue({ ...page, items: [] })
  mount()
  expect(await screen.findByText("没有可见看板")).toBeInTheDocument()
  expect(screen.queryByRole("alert")).toBeNull()
})
it("opens inline management and never opens a viewer during loading or saving", async () => {
  const user = await openManager()
  expect(state.openUrl).not.toHaveBeenCalled()
  const input = screen.getByRole("textbox", { name: "标题" })
  await user.clear(input)
  await user.type(input, "Revised title")
  await user.click(screen.getByRole("button", { name: "保存信息" }))
  await waitFor(() =>
    expect(state.executeDashboardMutation).toHaveBeenCalledTimes(1)
  )
  const request = state.executeDashboardMutation.mock.calls[0][0]
  expect(request.principal_id).toBe(identity.principal_id)
  expect(JSON.parse(request.body)).toEqual({
    title: "Revised title",
    description: "项目进展",
    expected_revision: 1,
  })
  expect(state.openUrl).not.toHaveBeenCalled()
})
it("preserves edited metadata after a revision conflict", async () => {
  state.executeDashboardMutation.mockRejectedValue(
    new DashboardApiError("revision_conflict", 409)
  )
  const user = await openManager()
  const input = screen.getByRole("textbox", { name: "标题" })
  await user.clear(input)
  await user.type(input, "Keep my edit")
  await user.click(screen.getByRole("button", { name: "保存信息" }))
  expect(await screen.findByRole("alert")).toHaveTextContent("看板已被更新")
  expect(input).toHaveValue("Keep my edit")
  expect(state.executeDashboardMutation).toHaveBeenCalledTimes(1)
})
it("keeps a 202 operation pending, queries it and retries the exact same request", async () => {
  state.executeDashboardMutation.mockResolvedValue({
    state: "processing",
    operation_id: "op",
    result: null,
    error: null,
  })
  const user = await openManager()
  await user.click(screen.getByRole("button", { name: "保存信息" }))
  await screen.findByText(/操作结果尚未确认/)
  expect(screen.queryByText("操作已完成，已刷新看板。")).toBeNull()
  await user.click(screen.getByRole("button", { name: "查询结果" }))
  await waitFor(() =>
    expect(state.getDashboardOperation).toHaveBeenCalledTimes(1)
  )
  await user.click(screen.getByRole("button", { name: "用原请求重试" }))
  await waitFor(() =>
    expect(state.executeDashboardMutation).toHaveBeenCalledTimes(2)
  )
  expect(state.executeDashboardMutation.mock.calls[1][0]).toBe(
    state.executeDashboardMutation.mock.calls[0][0]
  )
})
it("clears the management panel and pending operation when the identity changes", async () => {
  state.executeDashboardMutation.mockResolvedValue({
    state: "processing",
    operation_id: "op",
    result: null,
    error: null,
  })
  const user = await openManager()
  await user.click(screen.getByRole("button", { name: "保存信息" }))
  await screen.findByText(/操作结果尚未确认/)
  localStorage.setItem("codeg_token", "token-b")
  state.listDashboards.mockResolvedValue({
    ...page,
    items: [],
    identity: { ...identity, principal_id: "user-b" },
    principal_id: "user-b",
  })
  fireEvent(window, new Event("aresclaw:auth-changed"))
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "保存信息" })).toBeNull()
  )
  expect(screen.queryByRole("button", { name: "用原请求重试" })).toBeNull()
})
it("creates a blank draft without publishing or opening its HTML", async () => {
  state.executeDashboardMutation.mockResolvedValue({
    state: "succeeded",
    operation_id: "op",
    result: { dashboard_id: "new-board", revision: 1, status: "draft" },
    error: null,
  })
  const user = userEvent.setup()
  mount()
  await user.click(await screen.findByRole("button", { name: "新建看板" }))
  await user.type(screen.getByRole("textbox", { name: "标题" }), "New draft")
  await user.click(screen.getByRole("button", { name: "创建草稿" }))
  await waitFor(() =>
    expect(state.executeDashboardMutation).toHaveBeenCalledTimes(1)
  )
  expect(state.executeDashboardMutation.mock.calls[0][0].path).toBe(
    "/dashboards/drafts"
  )
  expect(state.openUrl).not.toHaveBeenCalled()
})
it("previews only on a deliberate link click and publishes the selected draft after confirmation", async () => {
  const user = await openManager()
  await user.click(screen.getByRole("tab", { name: "内容与版本" }))
  const preview = await screen.findByRole("link", { name: "预览草稿" })
  expect(state.openUrl).not.toHaveBeenCalled()
  await user.click(preview)
  expect(state.openUrl).toHaveBeenCalledWith(
    "https://boards.internal/dashboards/board-1?version=v2"
  )
  await user.click(screen.getByRole("button", { name: "发布草稿" }))
  expect(state.executeDashboardMutation).not.toHaveBeenCalled()
  const confirm = screen.getByRole("alertdialog")
  expect(confirm).toHaveTextContent("替换线上版本 1")
  await user.click(within(confirm).getByRole("button", { name: "确认" }))
  await waitFor(() => expect(state.executeDashboardMutation).toHaveBeenCalled())
  expect(
    JSON.parse(state.executeDashboardMutation.mock.calls[0][0].body)
  ).toEqual({ version_id: "v2", expected_revision: 1 })
})
it("restores an archived board to draft and offers no content preview while archived", async () => {
  state.getDashboard.mockResolvedValue({ ...board, status: "archived" })
  const user = await openManager()
  await user.click(screen.getByRole("tab", { name: "内容与版本" }))
  await screen.findByText(/所有者需先恢复为草稿/)
  expect(screen.queryByRole("link", { name: "预览草稿" })).toBeNull()
  await user.click(screen.getByRole("tab", { name: "概览" }))
  await user.click(screen.getByRole("button", { name: "恢复为草稿" }))
  const confirmation = screen.getByRole("alertdialog")
  expect(confirmation).toHaveTextContent("仍然不会公开")
  await user.click(within(confirmation).getByRole("button", { name: "确认" }))
  await waitFor(() => expect(state.executeDashboardMutation).toHaveBeenCalled())
  expect(state.executeDashboardMutation.mock.calls[0][0].path).toBe(
    "/dashboards/board-1/restore"
  )
})
it("opens a deep-linked dashboard in the current list and authorizes it", async () => {
  window.history.replaceState(
    null,
    "",
    "/workspace?view=dashboards&dashboard=board-1"
  )
  mount()
  await screen.findByRole("button", { name: "保存信息" })
  expect(state.getDashboard).toHaveBeenCalledWith("board-1")
  expect(state.openUrl).not.toHaveBeenCalled()
})

it("retains a pending write across workbench route unmount and remount", async () => {
  state.executeDashboardMutation.mockResolvedValue({
    state: "processing",
    operation_id: "op",
    result: null,
    error: null,
  })
  const user = userEvent.setup()
  const firstRoute = mount()
  await user.click(await screen.findByRole("button", { name: "管理看板" }))
  await user.click(await screen.findByRole("button", { name: "保存信息" }))
  await screen.findByText(/操作结果尚未确认/)
  const original = state.executeDashboardMutation.mock.calls[0][0]
  firstRoute.unmount()
  mount()
  await screen.findByText(/操作结果尚未确认/)
  await user.click(screen.getByRole("button", { name: "用原请求重试" }))
  await waitFor(() =>
    expect(state.executeDashboardMutation).toHaveBeenCalledTimes(2)
  )
  expect(state.executeDashboardMutation.mock.calls[1][0]).toBe(original)
})

it("preserves a failed retryable operation and resumes the same frozen request", async () => {
  state.executeDashboardMutation.mockResolvedValueOnce({
    state: "failed",
    operation_id: "op",
    result: null,
    error: { code: "operation_interrupted", retryable: true },
  })
  const user = await openManager()
  await user.click(screen.getByRole("button", { name: "保存信息" }))
  await screen.findByText(/操作结果尚未确认/)
  const original = state.executeDashboardMutation.mock.calls[0][0]
  expect(screen.getByRole("button", { name: "查询结果" })).toBeEnabled()
  await user.click(screen.getByRole("button", { name: "用原请求重试" }))
  await waitFor(() =>
    expect(state.executeDashboardMutation).toHaveBeenCalledTimes(2)
  )
  expect(state.executeDashboardMutation.mock.calls[1][0]).toBe(original)
})

it.each(["new draft", "another board"])(
  "does not show an unrelated completed notice when opening %s",
  async (target) => {
    const other = { ...board, id: "board-2", title: "Another dashboard" }
    const user = await openManager()
    await user.click(screen.getByRole("button", { name: "保存信息" }))
    await screen.findByText("操作已完成，已刷新看板。")
    state.listDashboards.mockResolvedValue({ ...page, items: [board, other] })
    await user.click(screen.getByRole("button", { name: "Close" }))
    if (target === "new draft") {
      await user.click(screen.getByRole("button", { name: "新建看板" }))
      await screen.findByRole("button", { name: "创建草稿" })
    } else {
      await user.click(screen.getByRole("button", { name: "刷新" }))
      const heading = await screen.findByRole("heading", { name: other.title })
      await user.click(
        within(heading.closest("article")!).getByRole("button", {
          name: "管理看板",
        })
      )
      await screen.findByRole("button", { name: "保存信息" })
    }
    expect(screen.queryByText("操作已完成，已刷新看板。")).toBeNull()
  }
)

it("shows resolved grant names and retains the stable ID when editing access", async () => {
  state.listDashboardGrants.mockResolvedValue({
    items: [
      {
        subject_type: "group",
        subject_id: "group-1",
        subject_name: "研发组",
        role: "viewer",
        starts_at: null,
        expires_at: null,
      },
    ],
    revision: 1,
  })
  const user = await openManager()
  await user.click(screen.getByRole("tab", { name: "访问权限" }))
  expect(await screen.findByText("研发组 · 查看者")).toBeInTheDocument()
  expect(screen.getByText("group-1")).toBeInTheDocument()
  expect(screen.getByRole("button", { name: "搜索名称" })).toBeInTheDocument()
  await user.click(screen.getByRole("button", { name: "编辑 group-1 的授权" }))
  expect(screen.getByText("群组 · 研发组")).toBeInTheDocument()
  await user.click(screen.getByRole("button", { name: "保存授权" }))
  await waitFor(() =>
    expect(state.executeDashboardMutation).toHaveBeenCalledTimes(1)
  )
  expect(
    JSON.parse(state.executeDashboardMutation.mock.calls[0][0].body)
  ).toMatchObject({ subject_type: "group", subject_id: "group-1" })
})
