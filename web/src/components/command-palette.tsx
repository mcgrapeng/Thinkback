/** 全局 ⌘K 命令面板:搜索记忆 + 快速跳转。
 * 自管理 open state:监听 ⌘K / Ctrl+K 切换、监听 `open-command-palette` CustomEvent。
 * 静态数据,不做后端集成(见 spec 7.scope cut)。 */

import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { Command, Database, ListTodo, Search, Settings, ShieldAlert, ScrollText } from "lucide-react";
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

type Item =
  | { kind: "memory"; id: string; label: string }
  | { kind: "page"; to: string; label: string; icon: typeof Database }
  | { kind: "shortcut"; label: string; hint: string };

const PAGES: Item[] = [
  { kind: "page", to: "/", label: "总览 Overview", icon: Command },
  { kind: "page", to: "/memories", label: "记忆浏览器 Memory Browser", icon: Database },
  { kind: "page", to: "/tasks", label: "任务监控 Tasks", icon: ListTodo },
  { kind: "page", to: "/govern", label: "治理操作 Govern", icon: ShieldAlert },
  { kind: "page", to: "/audit", label: "审计日志 Audit", icon: ScrollText },
  { kind: "page", to: "/config", label: "系统配置 Config", icon: Settings },
];

const SHORTCUTS: Array<{ keys: string; label: string; hint: string }> = [
  { keys: "⌘K", label: "命令面板", hint: "搜索记忆 / 跳转页面" },
  { keys: "?", label: "快捷键列表", hint: "查看所有可用快捷键" },
  { keys: "g o", label: "跳到总览", hint: "g 然后 o" },
  { keys: "g m", label: "跳到记忆浏览器", hint: "g 然后 m" },
  { keys: "g t", label: "跳到任务监控", hint: "g 然后 t" },
  { keys: "g g", label: "跳到治理操作", hint: "g 然后 g" },
  { keys: "g a", label: "跳到审计日志", hint: "g 然后 a" },
  { keys: "g c", label: "跳到系统配置", hint: "g 然后 c" },
  { keys: "r", label: "刷新当前页", hint: "R 键(总览/任务/审计)" },
  { keys: "esc", label: "关闭抽屉/弹窗", hint: "Esc" },
];

const RECENT_MEMORIES = [
  { id: "mem_001", label: "用户偏好使用极简暗色主题" },
  { id: "mem_002", label: "用户对海鲜过敏" },
];

export function CommandPalette() {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    const onCustom = () => setOpen((v) => !v);
    window.addEventListener("keydown", onKey);
    window.addEventListener("open-command-palette", onCustom);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("open-command-palette", onCustom);
    };
  }, []);

  useEffect(() => {
    if (!open) setQuery("");
  }, [open]);

  const results = useMemo<Item[]>(() => {
    if (needle.trim() === "?") {
      return SHORTCUTS.map((s) => ({ kind: "shortcut" as const, label: `${s.keys}  ${s.label}`, hint: s.hint }));
    }
    const mems: Item[] = RECENT_MEMORIES
      .filter((m) => !needle || m.id.includes(needle) || m.label.toLowerCase().includes(needle))
      .map((m) => ({ kind: "memory", id: m.id, label: `${m.id}  ${m.label}` }));
    const pages: Item[] = PAGES.filter((p) => !needle || p.label.toLowerCase().includes(needle));
    const shortcuts: Item[] = SHORTCUTS.filter(
      (s) => !needle || s.label.includes(needle) || s.hint.includes(needle),
    ).map((s) => ({ kind: "shortcut" as const, label: `${s.keys}  ${s.label}`, hint: s.hint }));
    return [...shortcuts, ...mems, ...pages];
  }, [needle]);

  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogContent className="max-w-[560px] gap-0 p-0 rounded-3xl border-border shadow-lg top-[20%] translate-y-0">
        <AlertDialogTitle className="sr-only">命令面板</AlertDialogTitle>
        <div className="flex items-center gap-3 border-b border-border-muted px-5 py-4">
          <Search aria-hidden="true" className="size-5 text-foreground-muted" />
          <input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索记忆 / 跳转页面..."
            className="flex-1 bg-transparent text-base outline-none placeholder:text-foreground-soft"
            aria-label="命令面板搜索"
          />
          <kbd className="hidden md:inline-flex items-center gap-1 rounded-md border border-border bg-background px-2 py-0.5 text-xs text-foreground-muted">esc</kbd>
        </div>
        <ul className="max-h-80 overflow-y-auto p-2">
          {results.length === 0 ? (
            <li className="px-4 py-8 text-center text-sm text-foreground-muted">没有匹配的结果</li>
          ) : (
            results.map((item, i) => (
              <li key={i}>
                <button
                  type="button"
                  className="flex w-full items-center gap-3 rounded-xl px-4 py-3 text-left text-sm hover:bg-background-muted focus-visible:bg-background-muted focus-visible:outline-none"
                  onClick={() => {
                    if (item.kind === "page") navigate({ to: item.to });
                    setOpen(false);
                  }}
                >
                  {item.kind === "memory" ? (
                    <Database aria-hidden="true" className="size-4 text-foreground-muted" />
                  ) : item.kind === "page" ? (
                    <item.icon aria-hidden="true" className="size-4 text-foreground-muted" />
                  ) : (
                    <kbd className="rounded-md border border-border bg-background px-2 py-0.5 font-mono text-[10px] text-foreground-emphasis">key</kbd>
                  )}
                  <span className="flex-1 truncate text-sm text-foreground-emphasis">{item.label}</span>
                  {item.kind === "shortcut" ? (
                    <span className="text-xs text-foreground-soft">{item.hint}</span>
                  ) : null}
                </button>
              </li>
            ))
          )}
        </ul>
        <div className="border-t border-border-muted px-5 py-2.5 flex items-center justify-between text-xs text-foreground-muted">
          <span>RECENT · QUICK NAVIGATION</span>
          <span>↑↓ 选择 · ↵ 跳转</span>
        </div>
      </AlertDialogContent>
    </AlertDialog>
  );
}
