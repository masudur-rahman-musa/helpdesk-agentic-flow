"use client";

import { useEffect, useState } from "react";

import { buttonVariants } from "@/components/ui/button";

/**
 * Dark mode: the system preference is the starting value, a stored choice
 * overrides it, and the class goes on `<html>` because Tailwind is configured
 * `darkMode: "class"` (see `tailwind.config.ts`).
 *
 * Two separate obligations, and a toggle can satisfy one without the other:
 * flipping the class on the document root (`tests/shell.test.tsx`, "flips the
 * dark class on the document root and back") and writing the choice to storage
 * so the next navigation keeps it ("persists the chosen theme under
 * helpdesk-theme"). The storage key is exported so the test pins it by name.
 */

export const THEME_STORAGE_KEY = "helpdesk-theme";

export type Theme = "light" | "dark";

function storedTheme(): Theme | null {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return value === "dark" || value === "light" ? value : null;
  } catch {
    // A browser with storage blocked still gets a working toggle, just one
    // that does not remember. Never a thrown render.
    return null;
  }
}

function systemTheme(): Theme {
  if (typeof window.matchMedia !== "function") {
    return "light";
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("light");

  // Resolved after mount, never during render: the server has no window, and
  // reading storage or a media query while rendering is what makes the markup
  // disagree with the client on the first paint.
  useEffect(() => {
    const resolved = storedTheme() ?? systemTheme();
    applyTheme(resolved);
    setTheme(resolved);
  }, []);

  function toggle(): void {
    const next: Theme = theme === "dark" ? "light" : "dark";
    applyTheme(next);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // Same as above: an unavailable store costs persistence, not the flip.
    }
    setTheme(next);
  }

  const next: Theme = theme === "dark" ? "light" : "dark";

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={`Switch to ${next} theme`}
      className={buttonVariants({ variant: "ghost", size: "sm" })}
    >
      <span aria-hidden="true">{theme === "dark" ? "Light" : "Dark"}</span>
    </button>
  );
}
