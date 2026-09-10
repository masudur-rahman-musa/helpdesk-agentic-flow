import type { Metadata } from "next";

import { QueryProvider } from "@/components/query-provider";
import { THEME_STORAGE_KEY } from "@/components/theme-toggle";

import "./globals.css";

export const metadata: Metadata = {
  title: "Helpdesk",
  description: "Ask for access in plain language. Every decision is auditable.",
};

/**
 * Applies the stored or preferred theme BEFORE the first paint. Without it the
 * page renders light and then flips, because the toggle can only read storage
 * after mount. Inline and tiny on purpose: anything async is too late.
 */
const THEME_BOOTSTRAP = `
try {
  var stored = window.localStorage.getItem(${JSON.stringify(THEME_STORAGE_KEY)});
  var dark = stored === 'dark' ||
    (stored === null && window.matchMedia('(prefers-color-scheme: dark)').matches);
  if (dark) document.documentElement.classList.add('dark');
} catch (e) {}
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP }} />
      </head>
      <body>
        <QueryProvider>{children}</QueryProvider>
      </body>
    </html>
  );
}
