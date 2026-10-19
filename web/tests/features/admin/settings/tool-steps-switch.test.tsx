import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueryProvider } from "@/components/layout/query-provider";
import { ToolStepsSwitch } from "@/features/admin/settings/components/tool-steps-switch";
import { FAILURE_MESSAGES } from "@/lib/failure-messages";

/**
 * `tool-steps-switch.test.tsx`: the one switch on the Settings page, against a
 * stubbed `fetch`. jsdom, no agent-api.
 *
 * Shapes this file fixes:
 * - `ToolStepsSwitch` takes no props. It reads the current value from the
 *   shared `/api/me` read (`show_tool_steps`), so the page and the chat always
 *   agree.
 * - it is a `role="switch"` named `Show tool steps in chat`, with `aria-checked`.
 * - a click sends `POST /api/admin/settings` with the body exactly
 *   `{"show_tool_steps": <the opposite of what the switch shows>}`.
 * - on success the `me` read is fetched again and the switch shows what the
 *   server now says. There is no optimistic flip.
 * - a refusal is a `role="alert"` with the sentence for the typed code, and the
 *   switch keeps its old value.
 */

const SETTINGS_PATH = "/api/admin/settings";
const SWITCH_NAME = "Show tool steps in chat";

const fetchStub = vi.fn<typeof fetch>();
let serverValue = false;
let postAnswer: () => Response | Promise<Response>;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") {
    return input;
  }
  return input instanceof URL ? input.toString() : input.url;
}

function posts(): { url: string; init: RequestInit }[] {
  return fetchStub.mock.calls
    .map(([input, init]) => ({ url: urlOf(input), init: init ?? {} }))
    .filter((call) => call.url.includes(SETTINGS_PATH) && call.init.method === "POST");
}

beforeEach(() => {
  serverValue = false;
  postAnswer = () => json({ show_tool_steps: !serverValue });
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input, init) => {
    const url = urlOf(input);
    if (url.includes("/api/me")) {
      return json({
        display_name: "Fake Admin One",
        principals: ["platform_admin"],
        tabs: ["chat", "settings"],
        show_tool_steps: serverValue,
      });
    }
    if (url.includes(SETTINGS_PATH) && init?.method === "POST") {
      const answer = await postAnswer();
      if (answer.ok) {
        serverValue = (JSON.parse(String(init.body)) as { show_tool_steps: boolean })
          .show_tool_steps;
      }
      return answer;
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderSwitch(): void {
  render(
    <QueryProvider>
      <ToolStepsSwitch />
    </QueryProvider>,
  );
}

describe("ToolStepsSwitch", () => {
  it.each([
    { current: false, sends: true },
    { current: true, sends: false },
  ])(
    "shows the current value and posts the opposite when clicked (from $current)",
    async ({ current, sends }) => {
      // Catches a switch that posts a constant (always true) or the value it
      // already shows, and one that never reads `show_tool_steps` from `me`
      // (it would show off for a tenant that has it on). It also catches a
      // switch that does not fetch `me` again after the change: the stub's
      // server value flips on the POST, so a stale switch keeps the old state.
      serverValue = current;
      const user = userEvent.setup();
      renderSwitch();

      const control = await screen.findByRole("switch", { name: SWITCH_NAME });
      await waitFor(() => expect(control).toHaveAttribute("aria-checked", String(current)));
      await user.click(control);

      await waitFor(() => expect(posts()).toHaveLength(1));
      const body = posts()[0]?.init.body;
      expect(JSON.parse(String(body))).toEqual({ show_tool_steps: sends });
      await waitFor(() =>
        expect(screen.getByRole("switch", { name: SWITCH_NAME })).toHaveAttribute(
          "aria-checked",
          String(sends),
        ),
      );
    },
  );

  it("shows the typed error and keeps the old value when the post fails", async () => {
    // Catches an optimistic flip that is never undone, and a free-text error:
    // the sentence must be the shared one for the typed code. The switch starts
    // off and the server refuses, so the only way to read "on" afterwards is a
    // flip the failure did not roll back.
    postAnswer = () => json({ code: "FORBIDDEN" }, 403);
    const user = userEvent.setup();
    renderSwitch();

    const control = await screen.findByRole("switch", { name: SWITCH_NAME });
    await waitFor(() => expect(control).toHaveAttribute("aria-checked", "false"));
    await user.click(control);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(FAILURE_MESSAGES.FORBIDDEN);
    expect(screen.getByRole("switch", { name: SWITCH_NAME })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    expect(posts()).toHaveLength(1);
  });
});
