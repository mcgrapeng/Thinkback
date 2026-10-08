import * as React from "react";
import { cn } from "@/lib/utils";

function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "flex h-10 w-full rounded-xl border border-input bg-card px-3.5 py-1 text-sm transition-shadow placeholder:text-foreground-soft focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--ring)] focus-visible:shadow-[0_0_0_3px_rgb(60_41_17/0.08)] disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    />
  );
}

export { Input };
