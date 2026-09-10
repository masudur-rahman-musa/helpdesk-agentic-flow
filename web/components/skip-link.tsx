/**
 * The first focusable element on every page. It has to be FIRST in the DOM,
 * not merely present: a skip link behind the banner, the nav and the theme
 * toggle takes four tabs to reach, which is the thing it exists to avoid.
 * Pinned by `tests/shell.test.tsx`'s "makes the skip link the first focusable
 * element in the shell".
 *
 * Visually hidden until focused, then drawn over the page.
 */
export function SkipLink() {
  return (
    <a
      href="#main-content"
      className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-md focus:bg-primary focus:px-3 focus:py-2 focus:text-primary-foreground focus:outline-none focus:ring-2 focus:ring-ring"
    >
      Skip to main content
    </a>
  );
}
