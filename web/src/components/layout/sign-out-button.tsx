"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { buttonVariants } from "@/components/ui/button";
import { logOut } from "@/features/auth/api";

/** The cookie is `httpOnly`, so only the server can expire it on sign-out. */
export function SignOutButton({ menuItem = false }: { menuItem?: boolean }) {
  const router = useRouter();
  const [pending, setPending] = useState(false);

  async function signOut(): Promise<void> {
    setPending(true);
    try {
      await logOut();
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
