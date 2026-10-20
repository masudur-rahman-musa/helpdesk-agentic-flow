import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";

// Tests share one window, so a value one test keeps in session storage
// (the open chat) must not reach the next test. Server-side tests have no window.
afterEach(() => {
  if (typeof window !== "undefined") {
    window.sessionStorage.clear();
  }
});
