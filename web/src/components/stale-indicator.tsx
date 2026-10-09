/** 数据新鲜度指示器:从某 timestamp 算 elapsed seconds,每 1s 重渲染。
 * 用于"数据 X 秒前更新"实时滚动显示。 */

import { useEffect, useState } from "react";
import { cn } from "@/lib/utils";

function formatElapsed(seconds: number): string {
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function StaleIndicator({
  updatedAt,
  className,
  prefix = "更新于",
}: {
  updatedAt: number | null | undefined;
  className?: string;
  prefix?: string;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  if (!updatedAt) return null;
  const elapsed = Math.max(0, Math.floor((now - updatedAt) / 1000));
  const isStale = elapsed > 30;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 tabular-nums text-xs",
        isStale ? "text-warning" : "text-foreground-muted",
        className,
      )}
      title={new Date(updatedAt).toLocaleString("zh-CN", { hour12: false })}
    >
      <span
        aria-hidden="true"
        className={cn(
          "inline-block size-1.5 rounded-full",
          isStale ? "bg-warning" : "bg-success",
        )}
      />
      {prefix} {formatElapsed(elapsed)}
    </span>
  );
}
