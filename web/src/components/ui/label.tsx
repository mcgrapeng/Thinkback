import * as React from "react";
import { cn } from "@/lib/utils";

/** 表单标签（原生 label + htmlFor；Radix Label 的气泡行为对本表单集无增益）。 */
function Label({ className, ...props }: React.ComponentProps<"label">) {
  return (
    <label
      data-slot="label"
      className={cn("text-xs font-medium text-foreground", className)}
      {...props}
    />
  );
}

export { Label };
