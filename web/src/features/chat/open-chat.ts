const OPEN_CHAT_KEY = "helpdesk.open-chat";

/** The chat open in this browser tab, or null. Storage can be blocked, so it never throws. */
export function readOpenChat(): string | null {
  try {
    return window.sessionStorage.getItem(OPEN_CHAT_KEY);
  } catch {
    return null;
  }
}

/** Remember the open chat for this tab, or forget it on null. */
export function keepOpenChat(id: string | null): void {
  try {
    if (id === null) {
      window.sessionStorage.removeItem(OPEN_CHAT_KEY);
    } else {
      window.sessionStorage.setItem(OPEN_CHAT_KEY, id);
    }
  } catch {
    // Blocked storage only means the chat is not reopened on return.
  }
}
