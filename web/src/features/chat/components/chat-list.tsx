import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import type { ChatSummary } from "@/features/chat/components/types";

/**
 * The left column, on a lighter shade of the muted colour so it reads as its
 * own part: "New chat" and the past chats, in the order given (the
 * server sends newest first). `chats` is `undefined` while loading. A failed
 * list shows an alert and leaves "New chat" working.
 */
export function ChatList({
  chats,
  activeId,
  onOpen,
  onNew,
  failed,
}: {
  chats: ChatSummary[] | undefined;
  activeId: string | null;
  onOpen: (id: string) => void;
  onNew: () => void;
  failed: boolean;
}) {
  return (
    <nav
      aria-label="Chats"
      className="flex w-full flex-col gap-2 rounded-md bg-muted/50 p-2 lg:min-h-0 lg:w-64 lg:shrink-0"
    >
      <Button type="button" variant="secondary" onClick={onNew}>
        New chat
      </Button>
      {failed ? <Alert tone="destructive">The chat list could not be loaded.</Alert> : null}
      <ul className="flex max-h-48 min-h-0 flex-col gap-1 overflow-y-auto lg:max-h-none lg:flex-1">
        {(chats ?? []).map((chat) => (
          <li key={chat.id}>
            <button
              type="button"
              onClick={() => onOpen(chat.id)}
              aria-current={chat.id === activeId ? "true" : undefined}
              className="w-full truncate rounded-md px-3 py-2 text-left text-sm hover:bg-muted aria-[current=true]:bg-muted aria-[current=true]:font-medium"
            >
              {chat.title}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
