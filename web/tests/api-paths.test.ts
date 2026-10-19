import { readdirSync, statSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { API, apiPath } from "@/lib/api-paths";

const API_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "src", "app", "api");

function routeFiles(dir: string): string[] {
  const found: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) found.push(...routeFiles(full));
    else if (name === "route.ts") found.push(full);
  }
  return found;
}

// "src/app/api/approvals/[requestId]/approve/route.ts" -> "/api/approvals/{requestId}/approve"
function routePaths(): string[] {
  return routeFiles(API_ROOT)
    .map((file) => relative(API_ROOT, dirname(file)).split(sep).join("/"))
    .map((dir) => `/api/${dir}`.replace(/\[([^\]]+)\]/g, "{$1}"));
}

describe("api path list", () => {
  it("every listed path has a route file", () => {
    // Kill: catches a list entry whose path was misspelled, renamed or points at
    // a route that was never written, so a caller would fetch a 404.
    const onDisk = new Set(routePaths());
    const listed = Object.values(API);
    expect(listed).toHaveLength(23);
    const missing = listed.filter((path) => !onDisk.has(path));
    expect(missing).toEqual([]);
  });

  it("every route file is listed", () => {
    // Kill: catches a route file added (or left over) that nobody put on the list,
    // so callers would hand-write its string again. Also catches two names
    // sharing one path, because the listed paths must be 23 different ones.
    const listed = new Set<string>(Object.values(API));
    expect(listed.size).toBe(23);
    const onDisk = routePaths();
    expect(onDisk).toHaveLength(23);
    const unlisted = onDisk.filter((path) => !listed.has(path));
    expect(unlisted).toEqual([]);
  });

  it("apiPath fills the id spot and escapes it", () => {
    // Kill: catches a builder that pastes the id in raw (no encodeURIComponent),
    // or leaves the {requestId} marker in the result.
    expect(apiPath(API.approveRequest, { requestId: "a b" })).toBe("/api/approvals/a%20b/approve");
  });

  it("the chat list and chat history paths are fixed", () => {
    // Kill: catches a list path that drifts from the proxy route, or a history
    // path that carries the chat id in the string (the client adds
    // `?chat_id=`, so the listed path must stay bare).
    expect(API.chatList).toBe("/api/chat/chats");
    expect(API.chatMessages).toBe("/api/chat/messages");
  });

  it("apiPath leaves a no-id path alone", () => {
    // Kill: catches a builder that mangles, prefixes or throws on a static path
    // given no params.
    expect(apiPath(API.me)).toBe("/api/me");
  });

  it("apiPath refuses a missing id", () => {
    // Kill: catches a builder that silently returns a path with the marker still
    // in it (or "undefined") when the id value was not given.
    expect(() => apiPath(API.approveRequest)).toThrow();
    expect(() => apiPath(API.approveRequest, { notificationId: "x" })).toThrow();
  });
});
