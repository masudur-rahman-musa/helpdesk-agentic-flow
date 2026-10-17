"use client";

import { useEffect, useState } from "react";

import { buttonVariants } from "@/components/ui/button";

/** Dark mode: the system preference first, a stored choice over it. */

export const THEME_STORAGE_KEY = "helpdesk-theme";

export type Theme = "light" | "dark";

function storedTheme(): Theme | null {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return value === "dark" || value === "light" ? value : null;
  } catch {
    // Blocked storage costs memory of the choice, never a thrown render.
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

  // After mount only: reading it in render makes server and client markup differ.
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
      // Same as above.
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
