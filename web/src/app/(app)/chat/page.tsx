import { ChatPanel } from "@/features/chat/components/chat-panel";

/**
 * The chat route. A thin server wrapper; the panel makes its own calls, and the
 * group layout draws the shell.
 */
export default function ChatPage() {
  return <ChatPanel />;
}
