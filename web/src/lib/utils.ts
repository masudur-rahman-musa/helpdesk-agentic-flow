import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn/ui's class helper: the last conflicting Tailwind class wins. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
