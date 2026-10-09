/** 热力图:60 格 5×12 网格,每格颜色深浅代表数值大小,悬停 tooltip。
 * 用于总览 5min 吞吐可视化。底部带色带图例 + 时间轴标注。 */

import { useState } from "react";
import { cn } from "@/lib/utils";

export function Heatmap({
  data,
  rows = 5,
  cols = 12,
  className,
  ariaLabel,
  cellClassName,
  emptyHint = "过去 5min 无数据",
  showLegend = true,
}: {
  data: number[];
  rows?: number;
  cols?: number;
  className?: string;
  ariaLabel?: string;
  cellClassName?: string;
  emptyHint?: string;
  showLegend?: boolean;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const flat = data.slice(0, rows * cols);
  const max = Math.max(1, ...flat);
  if (flat.length === 0) {
    return (
      <div className={cn("text-center text-sm text-foreground-muted", className)}>
        {emptyHint}
      </div>
    );
  }
  return (
    <div className={cn("space-y-1.5", className)}>
      <div
        role="img"
        aria-label={ariaLabel}
        className="grid gap-1"
        style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
        onMouseLeave={() => setHover(null)}
      >
        {Array.from({ length: rows * cols }).map((_, i) => {
          const v = flat[i] ?? 0;
          const intensity = v / max;
          return (
            <div
              key={i}
              onMouseEnter={() => setHover(i)}
              className={cn(
                "aspect-square rounded transition-colors",
                v === 0
                  ? "bg-background-muted"
                  : "bg-info",
                cellClassName,
              )}
              style={
                v > 0
                  ? { opacity: 0.15 + intensity * 0.85 }
                  : undefined
              }
            />
          );
        })}
      </div>
      {/* 图例:色带 + 少→多 + 时间轴 */}
      {showLegend ? (
        <div className="flex items-center gap-2 text-[10px] text-foreground-soft">
          <span>少</span>
          <div className="flex h-2 flex-1 gap-px overflow-hidden rounded-full">
            {[0.15, 0.35, 0.55, 0.75, 1].map((o) => (
              <div
                key={o}
                className={cn("flex-1", cellClassName ?? "bg-info")}
                style={{ opacity: o }}
              />
            ))}
          </div>
          <span>多</span>
          <span className="ml-auto tabular-nums">-5min ← 现在</span>
        </div>
      ) : null}
      {hover !== null && flat[hover] !== undefined ? (
        <p className="text-xs text-foreground-muted tabular-nums">
          第 {hover + 1} 格 · {flat[hover]} 次
        </p>
      ) : null}
    </div>
  );
}
