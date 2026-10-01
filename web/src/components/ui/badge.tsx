import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/** appica 徽章规范：soft 同色相底 + emphasis 文字色，无边框。 */

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-md font-medium w-fit whitespace-nowrap",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground",
        secondary: "bg-secondary text-secondary-foreground",
        outline: "border border-border text-foreground",
        success: "bg-success-soft text-success",
        error: "bg-error-soft text-error",
        info: "bg-info-soft text-info",
        warning: "bg-warning-soft text-warning",
        neutral: "bg-neutral-soft text-neutral",
        violet: "bg-violet-soft text-violet",
      },
      size: {
        default: "px-2.5 py-1 text-xs",
        sm: "px-2 py-0.5 text-[11px]",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

function Badge({
  className,
  variant,
  size,
  ...props
}: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return <span data-slot="badge" className={cn(badgeVariants({ variant, size }), className)} {...props} />;
}

export { Badge, badgeVariants };
