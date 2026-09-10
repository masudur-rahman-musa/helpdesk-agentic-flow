"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

/**
 * One TanStack Query client per browser session. Created in state rather than
 * at module scope: a module-level client is shared between requests on the
 * server, which would leak one user's cached data into another's render.
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
