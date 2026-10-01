import { cn } from "@/lib/utils";

/** 骨架占位（加载态）；配合 aria-busy 由使用方声明。 */
function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("animate-shimmer rounded-md", className)} {...props} />;
}

export { Skeleton };
