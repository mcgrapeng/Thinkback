/** 通用空/错状态组件:大图标 + 标题 + 描述 + 可选操作。
 * 取代各页面的 "暂无" 灰白文本。 */

import type { LucideIcon } from "lucide-react";
import { AlertTriangle, Inbox, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type Variant = "empty" | "error";

const VARIANT_CONFIG: Record<
  Variant,
  { icon: LucideIcon; iconClass: string; ringClass: string }
> = {
  empty: {
    icon: Inbox,
    iconClass: "text-foreground-soft",
    ringClass: "bg-background-muted",
  },
  error: {
    icon: AlertTriangle,
    iconClass: "text-error",
    ringClass: "bg-error-soft/40",
  },
};

export function EmptyState({
  variant = "empty",
  title,
  description,
  icon,
  action,
  className,
  /** 错误专用:详细错误信息(可折叠/红色文本) */
  detail,
}: {
  variant?: Variant;
  title: string;
  description?: string;
  icon?: LucideIcon;
  action?: { label: string; onClick: () => void };
  className?: string;
  detail?: string;
}) {
  const config = VARIANT_CONFIG[variant];
  const Icon = icon ?? (variant === "error" ? XCircle : config.icon);
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-2xl border border-dashed border-border px-6 py-16 text-center",
        className,
      )}
    >
      <div
        className={cn(
          "flex size-14 items-center justify-center rounded-full",
          config.ringClass,
        )}
      >
        <Icon aria-hidden="true" className={cn("size-7", config.iconClass)} />
      </div>
      <h3 className="text-base font-semibold text-foreground-intense">{title}</h3>
      {description ? (
        <p className="max-w-md text-sm text-foreground-muted leading-relaxed">{description}</p>
      ) : null}
      {detail ? (
        <p className="max-w-md break-words font-mono text-xs text-error">{detail}</p>
      ) : null}
      {action ? (
        <Button variant="outline" size="sm" onClick={action.onClick} className="mt-2">
          {action.label}
        </Button>
      ) : null}
    </div>
  );
}
