import { StrictMode, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { Toaster } from "sonner";
import { Layout } from "@/components/layout";
import { ErrorBoundary } from "@/components/error-boundary";
import { OverviewPage } from "@/pages/overview";
import { TasksPage } from "@/pages/tasks";
import { MemoriesPage } from "@/pages/memories";
import { GovernPage } from "@/pages/govern";
import { AuditPage } from "@/pages/audit";
import { ConfigPage } from "@/pages/config";
import "@/index.css";

const rootRoute = createRootRoute({
  component: () => {
    // 路由切换后焦点移入主区（WCAG 2.4.3），页面 h1 自身承担标题朗读
    useEffect(() => {
      document.getElementById("main")?.focus();
    }, []);
    return (
      <Layout>
        <Outlet />
      </Layout>
    );
  },
});

const overviewRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: OverviewPage,
});

const tasksRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tasks",
  component: TasksPage,
});

const memoriesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/memories",
  component: MemoriesPage,
});

const governRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/govern",
  component: GovernPage,
});

const auditRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/audit",
  component: AuditPage,
});

const configRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/config",
  component: ConfigPage,
});

const routeTree = rootRoute.addChildren([
  overviewRoute,
  memoriesRoute,
  tasksRoute,
  governRoute,
  auditRoute,
  configRoute,
]);
const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, staleTime: 15_000 },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
        <Toaster richColors position="top-center" />
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
);
