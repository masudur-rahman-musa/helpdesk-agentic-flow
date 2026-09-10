import { AppShell } from "@/components/app-shell";

/**
 * Placeholder. An earlier change replaces this with the chat surface — streaming, tool
 * steps, decision cards, citations. What it proves today is the authenticated
 * shell: reaching this page at all means the session cookie was present and the
 * middleware let the request through.
 */
export default function ChatPage() {
  return (
    <AppShell>
      <h1 className="text-lg font-semibold">Chat</h1>
      <p className="mt-2 max-w-prose text-sm text-muted-foreground">
        The assistant arrives with the next change. You are signed in, which is what this page is
        here to show.
      </p>
    </AppShell>
  );
}
