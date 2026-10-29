import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueryProvider } from "@/components/layout/query-provider";
import { PoliciesReader } from "@/features/policies/components/policies-reader";

/**
 * `policies-page.test.tsx`: the page every signed-in person reads the
 * company's access rules on. A list of the live documents, and the chosen one
 * in full, headings drawn from each section's heading path. jsdom and a
 * stubbed `fetch`; no agent-api.
 */

const fetchStub = vi.fn<typeof fetch>();

const DOCUMENTS = [
  { slug: "fake-access", title: "Fake Access Policy", version: "v2" },
  { slug: "fake-leave", title: "Fake Leave Policy", version: "v1" },
];

function documentOf(slug: string) {
  if (slug === "fake-access") {
    return {
      slug,
      title: "Fake Access Policy",
      version: "v2",
      sections: [
        {
          heading_path: ["Fake Access Policy", "1. Purpose"],
          text: "Fake purpose text with **bold words**.",
        },
        {
          heading_path: ["Fake Access Policy", "2. Scope", "2.1 Who"],
          text: "- Fake item one\n- Fake item <b>two</b>",
        },
      ],
    };
  }
  return {
    slug,
    title: "Fake Leave Policy",
    version: "v1",
    sections: [{ heading_path: ["Fake Leave Policy", "1. Leave"], text: "Fake leave text." }],
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function urlOf(input: RequestInfo | URL): URL {
  const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  return new URL(raw, "http://localhost");
}

function asked(): string[] {
  return fetchStub.mock.calls.map(([input]) => {
    const url = urlOf(input);
    return `${url.pathname}${url.search}`;
  });
}

beforeEach(() => {
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = urlOf(input);
    if (url.pathname !== "/api/policies") {
      throw new Error(`unexpected fetch to ${url.href}`);
    }
    const slug = url.searchParams.get("slug");
    if (slug === "fake-missing") {
      return json({ code: "NOT_FOUND" }, 404);
    }
    return json({ documents: DOCUMENTS, document: slug === null ? null : documentOf(slug) });
  });
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function draw(doc: string | null) {
  return render(
    <QueryProvider>
      <PoliciesReader doc={doc} />
    </QueryProvider>,
  );
}

describe("PoliciesReader", () => {
  it("lists the documents, each linking to its own page", async () => {
    // Catches a list that drops a document, keeps the old order, or links
    // somewhere other than this page with the document named.
    draw("fake-access");

    const list = await screen.findByRole("navigation", { name: "Policy documents" });
    const links = within(list).getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual([
      "Fake Access Policy",
      "Fake Leave Policy",
    ]);
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/policies?doc=fake-access",
      "/policies?doc=fake-leave",
    ]);
    expect(links[0]).toHaveAttribute("aria-current", "page");
    expect(links[1]).not.toHaveAttribute("aria-current");
  });

  it("shows the chosen document's headings by depth and its text", async () => {
    // Catches a page that drops the headings, draws them all at one level,
    // repeats the document title as a section heading, or skips a parent
    // heading that has no text of its own (`2. Scope`).
    draw("fake-access");

    const article = await screen.findByRole("article", { name: "Fake Access Policy" });
    expect(within(article).getAllByRole("heading", { name: "Fake Access Policy" })).toHaveLength(1);
    expect(
      within(article).getByRole("heading", { level: 2, name: "Fake Access Policy" }),
    ).toBeVisible();
    expect(within(article).getByRole("heading", { level: 3, name: "1. Purpose" })).toBeVisible();
    expect(within(article).getByRole("heading", { level: 3, name: "2. Scope" })).toBeVisible();
    expect(within(article).getByRole("heading", { level: 4, name: "2.1 Who" })).toBeVisible();
    expect(within(article).getByText("bold words").tagName).toBe("STRONG");
    expect(within(article).getByText("Fake item one").tagName).toBe("LI");
    expect(asked().sort()).toEqual(["/api/policies", "/api/policies?slug=fake-access"]);
  });

  it("shows tags in the text as typed, never as HTML", async () => {
    // Catches a page that renders the policy text as HTML: a `<b>` must stay
    // the four characters a person typed.
    draw("fake-access");

    const article = await screen.findByRole("article", { name: "Fake Access Policy" });
    expect(within(article).getByText("Fake item <b>two</b>")).toBeVisible();
    expect(article.querySelector("b")).toBeNull();
  });

  it("opens the first document when none is chosen", async () => {
    // Catches a page that stays blank until a document is clicked.
    draw(null);

    expect(await screen.findByRole("article", { name: "Fake Access Policy" })).toBeVisible();
    await waitFor(() =>
      expect(asked().sort()).toEqual(["/api/policies", "/api/policies?slug=fake-access"]),
    );
  });

  it("says so when the chosen document is not there", async () => {
    // Catches a page that shows an empty article, or nothing at all, for a
    // document that is gone. The list still shows, so the person can pick another.
    draw("fake-missing");

    expect(await screen.findByRole("alert")).toHaveTextContent("That could not be found.");
    expect(screen.queryByRole("article")).toBeNull();
    const list = await screen.findByRole("navigation", { name: "Policy documents" });
    expect(within(list).getAllByRole("link")).toHaveLength(2);
  });
});
