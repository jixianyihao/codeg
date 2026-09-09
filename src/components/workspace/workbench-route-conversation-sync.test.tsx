import { fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import {
  WorkbenchRouteProvider,
  useWorkbenchRoute,
} from "@/contexts/workbench-route-context"
import { WorkbenchRouteConversationSync } from "./workbench-route-conversation-sync"

const tabs = vi.hoisted(() => ({
  activeTabId: null as string | null,
  remote: false,
}))
const consumeRemoteActivation = vi.hoisted(() =>
  vi.fn(() => {
    const remote = tabs.remote
    tabs.remote = false
    return remote
  })
)
vi.mock("@/contexts/tab-context", () => ({
  useTabStore: (selector: (state: typeof tabs) => unknown) => selector(tabs),
  useTabActions: () => ({ consumeRemoteActivation }),
}))
function Probe() {
  const { routeId, openConversations, setRoute } = useWorkbenchRoute()
  return (
    <>
      <span data-testid="route">{routeId}</span>
      <button onClick={openConversations}>Open conversation</button>
      <button onClick={() => setRoute("dashboards")}>Open dashboards</button>
    </>
  )
}
function App() {
  return (
    <WorkbenchRouteProvider>
      <WorkbenchRouteConversationSync />
      <Probe />
    </WorkbenchRouteProvider>
  )
}
beforeEach(() => {
  tabs.activeTabId = null
  tabs.remote = false
  window.history.replaceState(
    null,
    "",
    "/workspace?view=dashboards&dashboard=board-1"
  )
})
afterEach(() => window.history.replaceState(null, "", "/workspace"))
describe("dashboard entry with workspace tab initialization", () => {
  it("keeps the dashboard entry when startup restores the first tab, then honors later local activation", () => {
    const { rerender } = render(<App />)
    expect(screen.getByTestId("route")).toHaveTextContent("dashboards")
    tabs.activeTabId = "restored-tab"
    rerender(<App />)
    expect(screen.getByTestId("route")).toHaveTextContent("dashboards")
    tabs.activeTabId = "locally-opened-tab"
    rerender(<App />)
    expect(screen.getByTestId("route")).toHaveTextContent("conversations")
  })
  it("honors explicit user navigation even before the first tab appears", () => {
    const { rerender } = render(<App />)
    fireEvent.click(screen.getByText("Open conversation"))
    tabs.activeTabId = "new-tab"
    rerender(<App />)
    expect(screen.getByTestId("route")).toHaveTextContent("conversations")
  })
  it("keeps remote tab changes from hijacking the dashboard after startup", () => {
    tabs.activeTabId = "existing-tab"
    const { rerender } = render(<App />)
    tabs.remote = true
    tabs.activeTabId = "remote-tab"
    rerender(<App />)
    expect(screen.getByTestId("route")).toHaveTextContent("dashboards")
    tabs.activeTabId = "local-tab"
    rerender(<App />)
    expect(screen.getByTestId("route")).toHaveTextContent("conversations")
  })
})
