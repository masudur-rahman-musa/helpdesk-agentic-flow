import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import AppError from "@/app/(app)/error";
import AppLoading from "@/app/(app)/loading";
import AppNotFound from "@/app/(app)/not-found";
import RootNotFound from "@/app/not-found";

/**
 * The three standard Next.js screens
 * for the signed-in group — loading, error, not-found — and the root not-found
 * screen for unknown URLs. jsdom only, no agent-api, no backing service.
 *
 * Each default export is rendered directly; a component using `next/link`
 * renders as a plain <a> in jsdom, so no router mock is needed here.
 */

describe("loading screen", () => {
  it("test_loading_screen_shows_a_status", () => {
    // Catches a loading screen that renders plain text with no role, so a
    // screen reader never announces the wait: the element must be a status and
    // carry the accessible word "Loading".
    render(<AppLoading />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
  });
});

describe("error screen", () => {
  it("test_error_screen_shows_the_message_and_retries", async () => {
    // Catches an error screen that swallows the message or wires the button to
    // nothing: the thrown message must show, and clicking "Try again" must call
    // the reset prop Next.js hands it.
    const reset = vi.fn<() => void>();
    const user = userEvent.setup();
    render(<AppError error={new Error("boom happened")} reset={reset} />);

    expect(screen.getByText("boom happened")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(reset).toHaveBeenCalledTimes(1);
  });

  it("test_error_screen_without_a_reset_still_renders", () => {
    // Unhappy path: a no-op reset must not stop the screen rendering; catches a
    // screen that only draws its content when reset does something.
    render(<AppError error={new Error("boom happened")} reset={() => {}} />);
    expect(screen.getByText("boom happened")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});

describe("not-found screen", () => {
  it("test_not_found_screen_links_back_to_chat", () => {
    // Catches a not-found screen missing the message or linking somewhere other
    // than /chat: the "page not found" text must show and the link's href must
    // be exactly /chat.
    render(<AppNotFound />);
    expect(screen.getByText(/page not found/i)).toBeInTheDocument();
    expect(screen.getByRole("link")).toHaveAttribute("href", "/chat");
  });

  it("test_root_not_found_screen_shows_without_the_shell", () => {
    // Catches a root not-found that renders the shell (it sits above the signed-
    // in group, so it must not): the message shows but there is no navigation
    // landmark.
    render(<RootNotFound />);
    expect(screen.getByText(/page not found/i)).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).toBeNull();
  });
});
