"use client";

import { useEffect, useRef, useState } from "react";

import { orderedLabels } from "@/components/layout/role-header";
import { useMe } from "@/features/auth/api";
import { SignOutButton } from "@/components/layout/sign-out-button";
import { Card } from "@/components/ui/card";

/**
 * Name and role as a button that opens Sign out. A disclosure, not a menu: one
 * action needs no roving focus. Until `/api/me` answers, a plain Sign out shows,
 * so signing out never waits on that read.
 */
export function UserMenu() {
  const me = useMe();
  const [open, setOpen] = useState(false);
  const group = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    function onKeyDown(event: KeyboardEvent): void {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      }
    }
    function onPointerDown(event: PointerEvent): void {
      if (!group.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [open]);

  if (me === undefined) {
    return <SignOutButton />;
  }
  const labels = orderedLabels(me.principals).join(", ");
  return (
    <div
      ref={group}
      role="group"
      aria-label="Signed in user"
      className="relative"
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
          setOpen(false);
        }
      }}
    >
      <button
        ref={trigger}
        type="button"
        aria-expanded={open}
        aria-label={`${me.display_name}, ${labels}`}
        onClick={() => setOpen((value) => !value)}
        className="flex h-8 max-w-48 items-center gap-1.5 rounded-md px-2.5 text-xs text-muted-foreground hover:bg-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:max-w-80"
      >
        <span className="truncate font-medium text-foreground">{me.display_name}</span>
        <span aria-hidden="true">·</span>
        <span className="truncate">{labels}</span>
      </button>
      {open ? (
        <Card className="absolute right-0 top-full z-10 mt-1 w-40 p-1 shadow-md">
          <SignOutButton menuItem />
        </Card>
      ) : null}
    </div>
  );
}
