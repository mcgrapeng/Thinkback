import * as React from "react";
import * as SheetPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

const Sheet = SheetPrimitive.Root;
const SheetTrigger = SheetPrimitive.Trigger;
const SheetClose = SheetPrimitive.Close;
const SheetPortal = SheetPrimitive.Portal;

const SheetOverlay = React.forwardRef<
  React.ComponentRef<typeof SheetPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof SheetPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <SheetPrimitive.Portal>
    <SheetPrimitive.Overlay
      className={cn(
        "fixed inset-0 z-50 bg-black/60 data-[state=open]:animate-in data-[state=closed]:animate-out",
        className,
      )}
      {...props}
      ref={ref}
    />
  </SheetPrimitive.Portal>
));
SheetOverlay.displayName = SheetPrimitive.Overlay.displayName;

const SheetContent = React.forwardRef<
  React.ComponentRef<typeof SheetPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof SheetPrimitive.Content> & { side?: "top" | "bottom" | "left" | "right" }
>(({ side = "right", className, children, ...props }, ref) => (
  <SheetPortal>
    <SheetPrimitive.Content
      ref={ref}
      className={cn(
        "fixed z-50 flex flex-col gap-4 bg-background shadow-lg transition ease-in-out data-[state=closed]:duration-200 data-[state=open]:duration-300 data-[state=open]:animate-in data-[state=closed]:animate-out",
        side === "right" &&
          "inset-y-0 right-0 h-full w-full rounded-l-3xl border-l border-border bg-background p-8 data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right sm:max-w-[480px]",
        side === "left" &&
          "inset-y-0 left-0 h-full w-full rounded-r-3xl border-r border-border bg-background p-8 data-[state=closed]:slide-out-to-left data-[state=open]:slide-in-from-left sm:max-w-[480px]",
        side === "top" &&
          "inset-x-0 top-0 rounded-b-3xl border-b border-border bg-background p-8 data-[state=closed]:slide-out-to-top data-[state=open]:slide-in-from-top",
        side === "bottom" &&
          "inset-x-0 bottom-0 rounded-t-3xl border-t border-border bg-background p-8 data-[state=closed]:slide-out-to-bottom data-[state=open]:slide-in-from-bottom",
        className,
      )}
      {...props}
    >
      {children}
      <SheetPrimitive.Close className="absolute right-4 top-4 rounded-sm opacity-70 transition-opacity hover:opacity-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--ring)] disabled:pointer-events-none">
        <X aria-hidden="true" className="size-4" />
        <span className="sr-only">关闭</span>
      </SheetPrimitive.Close>
    </SheetPrimitive.Content>
  </SheetPortal>
));
SheetContent.displayName = SheetPrimitive.Content.displayName;

function SheetHeader({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("flex flex-col space-y-1.5 p-4", className)} {...props} />;
}

function SheetTitle({ className, ...props }: React.ComponentProps<"h2">) {
  return (
    <SheetPrimitive.Title
      className={cn("text-base font-semibold text-foreground", className)}
      {...props}
    />
  );
}

function SheetDescription({ className, ...props }: React.ComponentProps<"p">) {
  return (
    <SheetPrimitive.Description
      className={cn("text-xs text-foreground-muted", className)}
      {...props}
    />
  );
}

export {
  Sheet,
  SheetPortal,
  SheetOverlay,
  SheetTrigger,
  SheetClose,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
};
