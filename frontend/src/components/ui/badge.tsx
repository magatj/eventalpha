import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";

import { cn } from "@/lib/utils";

const badgeVariants = cva("inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-medium leading-none whitespace-nowrap", {
  variants: {
    variant: {
      default: "border-border bg-surface-2 text-muted",
      up: "border-up/30 bg-up/10 text-up",
      down: "border-down/30 bg-down/10 text-down",
      warn: "border-warn/30 bg-warn/10 text-warn",
      accent: "border-accent/30 bg-accent/10 text-accent",
      demo: "border-demo/40 bg-demo/15 text-demo",
    },
  },
  defaultVariants: { variant: "default" },
});

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
