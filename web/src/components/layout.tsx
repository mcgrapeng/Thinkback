/** 应用外壳：桌面侧边导航（分组 + active 指示条）/ 移动顶栏（汉堡 → Sheet 抽屉）、
 * 暗色切换、跳过链接、顶部 fetch 进度条。 */

import { useEffect, useState } from "react";
import { Link, useLocation } from "@tanstack/react-router";
import { useIsFetching } from "@tanstack/react-query";
import {
  Activity,
  Database,
  ListTodo,
  Menu,
  Moon,
  ScrollText,
  Settings,
  ShieldAlert,
  Sun,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

const NAV_GROUPS = [
  {
    label: "监控",
    items: [
      { to: "/", label: "总览", icon: Activity },
      { to: "/memories", label: "记忆浏览器", icon: Database },
      { to: "/tasks", label: "任务监控", icon: ListTodo },
    ],
  },
  {
    label: "治理",
    items: [
      { to: "/govern", label: "治理操作", icon: ShieldAlert },
      { to: "/audit", label: "审计日志", icon: ScrollText },
      { to: "/config", label: "系统配置", icon: Settings },
    ],
  },
] as const;

function useDarkMode() {
  const [dark, setDark] = useState(() =>
    typeof window !== "undefined"
      ? (localStorage.getItem("thinkback-admin-theme") ??
        (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) ===
        "dark"
      : false,
  );
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("thinkback-admin-theme", dark ? "dark" : "light");
  }, [dark]);
  return { dark, toggle: () => setDark((value) => !value) };
}

function BrandMark({ compact = false }: { compact?: boolean }) {
  return (
    <div className="flex items-center gap-2">
      <span
        aria-hidden="true"
        className="flex size-6 shrink-0 items-center justify-center rounded-md bg-primary text-xs font-semibold text-primary-foreground"
      >
        T
      </span>
      <p
        className={cn(
          "text-sm font-semibold tracking-tight text-foreground-intense",
          compact && "text-[0.8125rem]",
        )}
      >
        Thinkback
        <span className="font-normal text-foreground-muted"> · 治理台</span>
      </p>
    </div>
  );
}

function NavLink({
  to,
  label,
  icon: Icon,
  active,
  onNavigate,
}: {
  to: string;
  label: string;
  icon: typeof Activity;
  active: boolean;
  onNavigate?: () => void;
}) {
  return (
    <Link
      to={to}
      aria-current={active ? "page" : undefined}
      onClick={onNavigate}
      className={cn(
        "relative flex h-10 items-center gap-2.5 rounded-lg pl-4 pr-3 text-sm font-medium text-foreground-muted transition-colors hover:bg-background-muted hover:text-foreground-intense focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--ring)]",
        active && "bg-background-muted pl-3 text-foreground-intense font-semibold",
      )}
    >
      {active ? (
        <span
          aria-hidden="true"
          className="absolute left-0 top-1/2 h-5 w-[3px] -translate-y-1/2 rounded-r-full bg-primary"
        />
      ) : null}
      <Icon aria-hidden="true" className="size-4" />
      {label}
    </Link>
  );
}

function ThemeButton({ dark, toggle }: { dark: boolean; toggle: () => void }) {
  return (
    <Button
      variant="ghost"
      size="icon"
      className="size-11 text-foreground"
      onClick={toggle}
      aria-label={dark ? "切换到浅色模式" : "切换到深色模式"}
      aria-pressed={dark}
    >
      {dark ? <Sun aria-hidden="true" /> : <Moon aria-hidden="true" />}
    </Button>
  );
}

export function Layout({ children }: { children: React.ReactNode }) {
  const { dark, toggle } = useDarkMode();
  const location = useLocation();
  const [navOpen, setNavOpen] = useState(false);
  const isFetching = useIsFetching();

  const active = (to: string) =>
    to === "/" ? location.pathname === "/" : location.pathname.startsWith(to);

  const navList = (onNavigate?: () => void) => (
    <>
      {NAV_GROUPS.map((group) => (
        <div key={group.label} className="flex flex-col gap-0.5">
          <p className="px-3 pb-1 text-[0.6875rem] font-medium tracking-wider text-foreground-soft">
            {group.label}
          </p>
          {group.items.map(({ to, label, icon }) => (
            <NavLink
              key={to}
              to={to}
              label={label}
              icon={icon}
              active={active(to)}
              onNavigate={onNavigate}
            />
          ))}
        </div>
      ))}
    </>
  );

  return (
    <div className="min-h-screen bg-background">
      {/* 全局 fetch 进度条：任何 query 正在 refetch 时顶部出现 */}
      <div
        aria-hidden="true"
        className={cn(
          "fixed left-0 right-0 top-0 z-50 h-0.5 overflow-hidden bg-transparent transition-opacity",
          isFetching > 0 ? "opacity-100" : "opacity-0",
        )}
      >
        <div className="h-full w-1/3 origin-left animate-[fetch-bar_1.1s_ease-in-out_infinite] bg-primary" />
      </div>
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-md focus:bg-primary focus:px-3 focus:py-2 focus:text-primary-foreground"
      >
        跳到主内容
      </a>
      <div className="flex min-h-screen flex-col md:flex-row">
        {/* 移动顶栏：汉堡 + 标题 + 主题切换 */}
        <header className="sticky top-0 z-40 flex items-center justify-between border-b border-border bg-background px-2 py-1.5 md:hidden">
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="icon"
              className="size-11 text-foreground-muted"
              aria-label="打开导航菜单"
              aria-expanded={navOpen}
              onClick={() => setNavOpen(true)}
            >
              <Menu aria-hidden="true" />
            </Button>
            <BrandMark compact />
          </div>
          <ThemeButton dark={dark} toggle={toggle} />
        </header>

        {/* 桌面侧边栏 */}
        <nav
          aria-label="主导航"
          className="hidden shrink-0 flex-col gap-6 border-r border-border bg-background-subtle p-5 md:flex md:w-64"
        >
          <div className="px-1 pb-1 pt-1">
            <BrandMark />
          </div>
          {navList()}
          <div className="mt-auto px-1">
            <ThemeButton dark={dark} toggle={toggle} />
          </div>
        </nav>

        {/* 移动导航抽屉 */}
        <Sheet open={navOpen} onOpenChange={setNavOpen}>
          <SheetContent side="left" className="w-72 gap-0 p-0 sm:max-w-72">
            <SheetHeader className="border-b border-border pb-3">
              <SheetTitle className="sr-only">Thinkback 治理台</SheetTitle>
              <SheetDescription className="sr-only">主导航</SheetDescription>
              <BrandMark />
            </SheetHeader>
            <nav aria-label="移动导航" className="flex flex-col gap-4 overflow-y-auto p-3">
              {navList(() => setNavOpen(false))}
            </nav>
            <div className="mt-auto border-t border-border px-3 py-2">
              <ThemeButton dark={dark} toggle={toggle} />
            </div>
          </SheetContent>
        </Sheet>

        <main id="main" tabIndex={-1} className="min-w-0 flex-1 mx-auto w-full max-w-[1400px] p-4 md:p-6 lg:p-8">
          {children}
        </main>
      </div>
    </div>
  );
}
