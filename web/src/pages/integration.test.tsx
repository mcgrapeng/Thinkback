/** 集成页冒烟：API Key 列表/空态渲染 + mem0 配置区挂载。
 *  fetch 全量打桩，确定性无网络。 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IntegrationPage } from "./integration";

function stubFetch(routes: Record<string, unknown>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const payload = Object.entries(routes).find(([path]) => url.includes(path))?.[1] ?? [];
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
}

function renderPage(): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <IntegrationPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("IntegrationPage", () => {
  it("列出 API Key 卡片", async () => {
    stubFetch({
      "/admin/api/integration/keys": [
        {
          key_id: "key-smoke-1",
          tenant_id: "tenant-dev",
          scopes: ["memory:recall"],
          plan: "pro",
          rate_limit_per_minute: 60,
          rate_limit_burst: 120,
          is_active: true,
          created_at: "2026-05-04T10:00:00Z",
        },
      ],
      "/admin/api/mem0/configs": [],
      "/admin/api/mem0/sections": [],
    });
    renderPage();

    expect(await screen.findByText("API Keys")).toBeInTheDocument();
    expect(await screen.findByText("key-smoke-1")).toBeInTheDocument();
  });

  it("无 Key 时展示空态引导", async () => {
    stubFetch({
      "/admin/api/integration/keys": [],
      "/admin/api/mem0/configs": [],
      "/admin/api/mem0/sections": [],
    });
    renderPage();

    expect(await screen.findByText("还没有 API Key")).toBeInTheDocument();
  });

  it("加载失败展示错误态与重试", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("boom", { status: 500 })),
    );
    renderPage();

    expect(await screen.findByText("无法加载 API Key 列表")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "重试" }).length).toBeGreaterThan(0);
  });
});
