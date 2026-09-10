"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

/**
 * Ends the session by asking the server to expire the cookie. The browser
 * cannot clear it itself — that is what `httpOnly` buys — so a sign-out is a
 * POST to `/api/logout` and nothing else.
 */
export function SignOutButton() {
  const router = useRouter();
  const [pending, setPending] = useState(false);

  async function signOut(): Promise<void> {
    setPending(true);
    try {
      await fetch("/api/logout", { method: "POST" });
    } finally {
      setPending(false);
    }
    router.replace("/login");
    router.refresh();
  }

  return (
    <button
      type="button"
      onClick={() => void signOut()}
      disabled={pending}
      className="rounded-md border border-border px-2 py-1 text-xs text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
    >
      Sign out
    </button>
  );
}
