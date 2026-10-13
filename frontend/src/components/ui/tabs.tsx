import * as TabsPrimitive from "@radix-ui/react-tabs";
import * as React from "react";

import { cn } from "@/lib/utils";

export const Tabs = TabsPrimitive.Root;

export const TabsList = ({ className, ...p }: React.ComponentProps<typeof TabsPrimitive.List>) => (
  <TabsPrimitive.List className={cn("inline-flex flex-wrap items-center gap-1 rounded-lg border border-border bg-surface p-1", className)} {...p} />
);

export const TabsTrigger = ({ className, ...p }: React.ComponentProps<typeof TabsPrimitive.Trigger>) => (
  <TabsPrimitive.Trigger
    className={cn(
      "rounded-md px-3 py-1.5 text-xs font-medium text-muted transition-colors hover:text-foreground data-[state=active]:bg-surface-3 data-[state=active]:text-foreground",
      className,
    )}
    {...p}
  />
);

export const TabsContent = ({ className, ...p }: React.ComponentProps<typeof TabsPrimitive.Content>) => (
  <TabsPrimitive.Content className={cn("mt-4 focus-visible:outline-none", className)} {...p} />
);
