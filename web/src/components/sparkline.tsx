/** 轻量 sparkline 组件:纯 SVG,无依赖。给定一组数值,绘制 1px-2px 折线 + 可选末端高亮点。
 * 用于总览 KPI 趋势展示。 */

import { useId } from "react";
import { cn } from "@/lib/utils";

type SparklineProps = {
  data: number[];
  width?: number;
  height?: number;
  /** 描边色,默认用 --foreground-emphasis */
  strokeClass?: string;
  /** 末端高亮点颜色,默认 success 状态色 */
  endClass?: string;
  /** 是否绘制末端高亮(0/1 元素) */
  showEnd?: boolean;
  /** 是否填充淡渐变(0 → max) */
  fill?: boolean;
  className?: string;
  /** 是否平滑曲线(默认 true) */
  smooth?: boolean;
  ariaLabel?: string;
};

export function Sparkline({
  data,
  width = 120,
  height = 32,
  strokeClass = "stroke-foreground-emphasis",
  endClass = "fill-foreground-intense",
  showEnd = true,
  fill = false,
  className,
  smooth = true,
  ariaLabel,
}: SparklineProps) {
  const gradId = useId();
  if (data.length < 2) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const pad = 2;
  const w = width - pad * 2;
  const h = height - pad * 2;
  const points = data.map((v, i) => {
    const x = pad + (i / (data.length - 1)) * w;
    const y = pad + (1 - (v - min) / range) * h;
    return [x, y] as const;
  });
  const path = smooth
    ? buildSmoothPath(points)
    : points.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const fillPath = `${path} L${points[points.length - 1]![0].toFixed(1)},${height - pad} L${points[0]![0].toFixed(1)},${height - pad} Z`;
  const last = points[points.length - 1]!;
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      className={cn("block", className)}
      role="img"
      aria-label={ariaLabel}
    >
      {fill ? (
        <>
          <defs>
            <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="currentColor" stopOpacity="0.18" />
              <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
            </linearGradient>
          </defs>
          <path d={fillPath} fill={`url(#${gradId})`} />
        </>
      ) : null}
      <path
        d={path}
        fill="none"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        className={strokeClass}
      />
      {showEnd ? (
        <circle
          cx={last[0]}
          cy={last[1]}
          r="2.5"
          className={endClass}
        />
      ) : null}
    </svg>
  );
}

/** Catmull-Rom → Bezier 平滑路径,让 sparkline 看起来更顺。 */
function buildSmoothPath(pts: ReadonlyArray<readonly [number, number]>): string {
  if (pts.length < 2) return "";
  if (pts.length === 2) {
    const [a, b] = pts;
    return `M${a[0].toFixed(1)},${a[1].toFixed(1)} L${b[0].toFixed(1)},${b[1].toFixed(1)}`;
  }
  const tension = 0.18;
  let d = `M${pts[0]![0].toFixed(1)},${pts[0]![1].toFixed(1)}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i === 0 ? 0 : i - 1]!;
    const p1 = pts[i]!;
    const p2 = pts[i + 1]!;
    const p3 = pts[i + 2 < pts.length ? i + 2 : i + 1]!;
    const cp1x = p1[0] + (p2[0] - p0[0]) * tension;
    const cp1y = p1[1] + (p2[1] - p0[1]) * tension;
    const cp2x = p2[0] - (p3[0] - p1[0]) * tension;
    const cp2y = p2[1] - (p3[1] - p1[1]) * tension;
    d += ` C${cp1x.toFixed(1)},${cp1y.toFixed(1)} ${cp2x.toFixed(1)},${cp2y.toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
  }
  return d;
}
