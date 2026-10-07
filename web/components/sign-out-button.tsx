"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { buttonVariants } from "@/components/ui/button";

/**
 * Ends the session by asking the server to expire the cookie. The browser
 * cannot clear it itself — that is what `httpOnly` buys — so a sign-out is a
 * POST to `/api/logout` and nothing else.
 *
 * `menuItem` gives it the full-width look of a row in the user menu;
 * the behaviour is the same either way.
 */
export function SignOutButton({ menuItem = false }: { menuItem?: boolean }) {
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
      className={
        menuItem
          ? buttonVariants({ variant: "ghost", size: "sm", className: "w-full justify-start" })
          : buttonVariants({ variant: "secondary", size: "sm" })
      }
    >
      Sign out
    </button>
  );
}
