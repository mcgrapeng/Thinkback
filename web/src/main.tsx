import { StrictMode, Suspense, lazy, useEffect } from "react";
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
import { TooltipProvider } from "@/components/ui/tooltip";
import { OverviewPage } from "@/pages/overview";
import { SkeletonLines } from "@/components/ui/skeleton";
import "@/index.css";

const TasksPage = lazy(() => import("@/pages/tasks").then((m) => ({ default: m.TasksPage })));
const MemoriesPage = lazy(() => import("@/pages/memories").then((m) => ({ default: m.MemoriesPage })));
const GovernPage = lazy(() => import("@/pages/govern").then((m) => ({ default: m.GovernPage })));
const AuditPage = lazy(() => import("@/pages/audit").then((m) => ({ default: m.AuditPage })));
const ConfigPage = lazy(() => import("@/pages/config").then((m) => ({ default: m.ConfigPage })));

function PageFallback() {
  return (
    <div aria-busy="true" className="space-y-4">
      <SkeletonLines count={3} />
      <div className="h-48 rounded-2xl bg-background-muted animate-shimmer" />
    </div>
  );
}

const rootRoute = createRootRoute({
  component: () => {
    // 路由切换后焦点移入主区（WCAG 2.4.3），页面 h1 自身承担标题朗读
    useEffect(() => {
      document.getElementById("main")?.focus();
    }, []);
    return (
      <Layout>
        <Suspense fallback={<PageFallback />}>
          <Outlet />
        </Suspense>
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
const router = createRouter({
  routeTree,
  defaultViewTransition: true,
});

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
        <TooltipProvider delayDuration={200}>
          <RouterProvider router={router} />
          <Toaster richColors position="top-center" />
        </TooltipProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
);
