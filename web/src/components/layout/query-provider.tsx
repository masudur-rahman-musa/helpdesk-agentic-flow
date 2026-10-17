"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

/**
 * One query client per browser session, kept in state: a module-level client
 * is shared across server requests and would leak one user's data to another.
 */
export function QueryProvider({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
      }),
  );

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
