"use client";

import { useEffect, useRef, useState } from "react";

import { orderedLabels, useMe } from "@/components/layout/role-header";
import { SignOutButton } from "@/components/layout/sign-out-button";
import { Card } from "@/components/ui/card";

/**
 * The signed-in person's name and account type as one button that opens a
 * panel holding Sign out. A disclosure, not a `role="menu"`: one
 * action needs no roving focus (decision B).
 *
 * Until `/api/me` answers, after it failed and with no provider above, there
 * is no trigger and the plain Sign out button shows, so signing out never
 * depends on that read (decision C; `tests/user-menu.test.tsx::shows a plain
 * Sign out and no trigger while the read is pending`).
 *
 * Closes on Escape (focus returns to the trigger), a `pointerdown` outside the
 * group, a second click on the trigger and focus leaving the group (decision D;
 * `::closes on Escape and returns focus to the trigger`, `::closes on a
 * pointerdown outside the group`, `::closes on a second click on the trigger`,
 * `::closes when focus leaves the group`).
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
