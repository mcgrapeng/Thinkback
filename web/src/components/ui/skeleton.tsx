import { cn } from "@/lib/utils";

/** 骨架占位(加载态);配合 aria-busy 由使用方声明。
 *
 * Skeleton: 单行占位(任意宽高,rounded)。
 * SkeletonLines: 多行占位,自动按 100% / 85% / 70% / 55% 错位,模拟真实文本行。 */
function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      className={cn("animate-shimmer rounded-md bg-background-strong/60", className)}
      {...props}
    />
  );
}

const LINE_WIDTHS = ["w-full", "w-[85%]", "w-[70%]", "w-[55%]", "w-[40%]", "w-[92%]", "w-[65%]"];

function SkeletonLines({
  count = 3,
  gap = 2,
  lastShort = true,
  className,
}: {
  count?: number;
  gap?: 1 | 2 | 3 | 4;
  lastShort?: boolean;
  className?: string;
}) {
  return (
    <div
      aria-hidden="true"
      className={cn(`flex flex-col gap-${gap}`, className)}
    >
      {Array.from({ length: count }).map((_, i) => {
        const w = LINE_WIDTHS[i % LINE_WIDTHS.length]!;
        return (
          <Skeleton
            key={i}
            className={cn("h-3", w, lastShort && i === count - 1 && "w-1/2")}
          />
        );
      })}
    </div>
  );
}

export { Skeleton, SkeletonLines };
