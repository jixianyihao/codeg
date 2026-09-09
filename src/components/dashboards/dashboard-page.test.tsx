import { fireEvent, render, screen } from "@testing-library/react"
import { NextIntlClientProvider } from "next-intl"
import { beforeEach, expect, it, vi } from "vitest"
import { DashboardsPage } from "./dashboard-page"

const state = vi.hoisted(() => ({ call: vi.fn() }))
vi.mock("@/lib/transport", () => ({
  getTransport: () => ({ call: state.call }),
}))
vi.mock("@/lib/platform", () => ({ isDesktop: () => false }))

const messages = {
  Dashboards: {
    title: "看板",
    description: "查看和打开已发布看板",
    mine: "我创建的",
    shared: "分享给我",
    all: "全部可见",
    search: "搜索看板",
    refresh: "刷新",
    loading: "正在加载",
    empty: "没有可见看板",
    error: "无法加载看板",
    notConfigured: "请配置看板服务",
    open: "打开看板",
    copy: "复制链接",
    copied: "已复制",
    more: "加载更多",
    published: "已发布",
    archived: "已下架",
    status: "状态",
    owner: "所有者",
    editor: "编辑者",
    viewer: "查看者",
    service: "集成账号",
    expires: "访问到期",
    publishedAt: "发布时间",
    webOnly: "仅支持网页模式",
    invalidLink: "查看链接不可用",
  },
}
const page = {
  items: [
    {
      id: "board-1",
      title: "发布周报 <img src=x>",
      description: "项目进展",
      owner_principal_id: "a",
      owner_name: "CI",
      owner_type: "service",
      current_version_id: "v1",
      revision: 1,
      status: "published",
      role: "viewer",
      published_at: "2026-09-09T01:00:00Z",
      expires_at: null,
    },
  ],
  next_cursor: null,
  principal_id: "user-a",
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
  state.call.mockReset()
  localStorage.clear()
})

it("shows metadata cards with independent links and no HTML embeds", async () => {
  state.call.mockResolvedValue(page)
  const { container } = mount()
  expect(await screen.findByText("发布周报 <img src=x>")).toBeInTheDocument()
  expect(screen.getByRole("link", { name: "打开看板" })).toHaveAttribute(
    "href",
    "https://boards.internal/dashboards/board-1"
  )
  expect(container.querySelector("iframe, img")).toBeNull()
  expect(screen.getByText("查看者")).toBeInTheDocument()
})

it("shows service failure and allows a successful retry", async () => {
  state.call
    .mockRejectedValueOnce(new Error("unavailable"))
    .mockResolvedValue(page)
  mount()
  expect(await screen.findByRole("alert")).toHaveTextContent("无法加载看板")
  fireEvent.click(screen.getByRole("button", { name: "刷新" }))
  expect(await screen.findByText("项目进展")).toBeInTheDocument()
})

it("distinguishes an empty successful listing", async () => {
  state.call.mockResolvedValue({ ...page, items: [] })
  mount()
  expect(await screen.findByText("没有可见看板")).toBeInTheDocument()
  expect(screen.queryByRole("alert")).toBeNull()
})
