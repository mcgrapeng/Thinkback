/** 列表键盘导航 hook:j/k 上下移动、x 选中/取消、a 全选、Esc 清除。
 *
 * 用于 memories / tasks / audit 的编辑式键盘工作流。
 * 返回 selectedIndex(当前高亮)和 selectedIds(多选集合)。 */

import { useCallback, useEffect, useRef, useState } from "react";

export function useListKeyboardNavigation(totalItems: number) {
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const lastSelectedRef = useRef(0);

  // j/k 移动,x 切换选中,a 全选,Esc 清除选中
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // 只在非输入框/textarea 时响应
      const target = e.target as HTMLElement;
      if (
        target.tagName === "INPUT" ||
        target.tagName === "TEXTAREA" ||
        target.isContentEditable
      ) {
        return;
      }
      switch (e.key) {
        case "j":
        case "ArrowDown":
          e.preventDefault();
          setSelectedIndex((i) => Math.min(i + 1, totalItems - 1));
          break;
        case "k":
        case "ArrowUp":
          e.preventDefault();
          setSelectedIndex((i) => Math.max(i - 1, 0));
          break;
        case "Home":
          e.preventDefault();
          setSelectedIndex(0);
          break;
        case "End":
          e.preventDefault();
          setSelectedIndex(totalItems - 1);
          break;
        case "x":
        case " ":
          e.preventDefault();
          setSelectedIds((prev) => {
            const next = new Set(prev);
            const key = String(selectedIndex);
            if (next.has(key)) next.delete(key);
            else next.add(key);
            return next;
          });
          lastSelectedRef.current = selectedIndex;
          break;
        case "Escape":
          setSelectedIds(new Set());
          break;
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [totalItems, selectedIndex]);

  const selectAll = useCallback(() => {
    setSelectedIds(new Set(Array.from({ length: totalItems }, (_, i) => String(i))));
  }, [totalItems]);

  const clearSelection = useCallback(() => setSelectedIds(new Set()), []);

  return {
    selectedIndex,
    setSelectedIndex,
    selectedIds,
    setSelectedIds,
    selectAll,
    clearSelection,
  };
}
