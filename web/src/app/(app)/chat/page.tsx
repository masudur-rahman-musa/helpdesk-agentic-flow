import { ChatPanel } from "@/features/chat/components/chat-panel";

/**
 * The chat route. A thin server wrapper: everything it shows comes from the
 * two route handlers `ChatPanel` calls, so the page has nothing of its own to
 * fetch and the whole surface is testable without rendering a server
 * component (`tests/chat-page.test.tsx` renders the panel directly). The shell
 * is the group layout `(app)/layout.tsx`, so the page renders just
 * its content.
 *
 * Reaching this page at all means the session cookie was present — the
 * middleware refuses it otherwise — and the panel's own calls are
 * what discover a cookie whose token has since expired.
 */
export default function ChatPage() {
  return <ChatPanel />;
}
