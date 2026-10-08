/** 统一页头:大标题 + 描述 + 右侧操作槽(Editorial Premium 规格 text-4xl/600 + tracking-tighter)。 */

import { cn } from "@/lib/utils";

export function PageHeader({
  title,
  description,
  actions,
  className,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-wrap items-end justify-between gap-4 pb-6", className)}>
      <div className="min-w-0 space-y-2">
        <h1 className="text-3xl font-semibold tracking-tighter text-foreground-intense sm:text-4xl">
          {title}
        </h1>
        {description ? (
          <p className="max-w-2xl text-base text-foreground-muted leading-relaxed">
            {description}
          </p>
        ) : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </div>
  );
}
