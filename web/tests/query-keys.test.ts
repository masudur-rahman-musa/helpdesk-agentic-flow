import { describe, expect, it } from "vitest";

import { queryKeys } from "@/lib/query-keys";

/**
 * The one list of query keys. Fixed entries are arrays; the rest are
 * functions that build a key from an id or a filter.
 */

type Key = readonly unknown[];

function fixedKeys(): [string, Key][] {
  return Object.entries(queryKeys as Record<string, unknown>).filter(
    (entry): entry is [string, Key] => Array.isArray(entry[1]),
  );
}

function startsWith(key: Key, prefix: Key): boolean {
  return prefix.length <= key.length && prefix.every((part, i) => key[i] === part);
}

describe("query key list", () => {
  it("every fixed key is unique", () => {
    // Kill: catches two names sharing one key, so one screen's refresh or cache
    // entry would silently hit another screen's data.
    const fixed = fixedKeys();
    expect(fixed.length).toBeGreaterThanOrEqual(8);
    const joined = fixed.map(([, key]) => JSON.stringify(key));
    expect(new Set(joined).size).toBe(joined.length);
  });

  it("notification keys share one prefix", () => {
    // Kill: catches a page key or unread key that drifts off the shared prefix,
    // so marking one read would refresh the list but not the bell (or the reverse).
    expect(startsWith(queryKeys.notificationPage(true), queryKeys.notifications)).toBe(true);
    expect(startsWith(queryKeys.notificationPage(false), queryKeys.notifications)).toBe(true);
    expect(startsWith(queryKeys.unreadCount, queryKeys.notifications)).toBe(true);
  });

  it("review keys share one prefix", () => {
    // Kill: catches a queue key built outside the review prefix, so a decision
    // would not refresh the queue.
    expect(startsWith(queryKeys.reviewQueue("open"), queryKeys.myReviews)).toBe(true);
    expect(queryKeys.reviewQueue("open")).not.toEqual(queryKeys.reviewQueue("closed"));
  });

  it("each chat has its own history key, apart from the list", () => {
    // Kill: catches a history key that ignores the chat id, so opening a second
    // chat would show the first chat's cached lines, or one that collides with
    // the list key.
    expect(queryKeys.chatMessages("c-1")).not.toEqual(queryKeys.chatMessages("c-2"));
    expect(queryKeys.chatMessages("c-1")).not.toEqual(queryKeys.chatList);
    expect(startsWith(queryKeys.chatMessages("c-1"), ["chat"])).toBe(true);
    expect(startsWith(queryKeys.chatList, ["chat"])).toBe(true);
  });

  it("keys keep today's values", () => {
    // Kill: catches a renamed or reshaped key, which would split the cache the
    // screens share today.
    expect(queryKeys.me).toEqual(["me"]);
    expect(queryKeys.myApprovals).toEqual(["me", "approvals"]);
    expect(queryKeys.myAccess).toEqual(["me", "access"]);
    expect(queryKeys.myRequests).toEqual(["me", "requests"]);
    expect(queryKeys.unreadCount).toEqual(["notifications", "unread"]);
    expect(queryKeys.notifications).toEqual(["notifications"]);
    expect(queryKeys.chatList).toEqual(["chat", "list"]);
    expect(queryKeys.chatMessages("c-1")).toEqual(["chat", "messages", "c-1"]);
    expect(queryKeys.myReviews).toEqual(["me", "reviews"]);
    expect(queryKeys.notificationPage(true)).toEqual(["notifications", "page", true]);
    expect(queryKeys.reviewItems("r-1")).toEqual(["reviews", "r-1", "items"]);
  });
});
