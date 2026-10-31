"use client";

/**
 * The bubble shown while the assistant works and no reply text has arrived.
 * Three dots pulse a little apart; `motion-safe:` keeps them still for anyone
 * who asked their system for less motion. `words`, when given, say what the
 * assistant is doing right now ("Checking your access") beside the dots. The
 * status keeps its name, so a screen reader still hears that it is working.
 */
export function TypingDots({ words = "" }: { words?: string }) {
  return (
    <div
      role="status"
      aria-label="Assistant is working"
      className="flex items-center gap-1 self-start rounded-md bg-muted px-3 py-3"
    >
      <span className="sr-only">Assistant is working</span>
      {[0, 1, 2].map((index) => (
        <span
          key={index}
          aria-hidden="true"
          style={{ animationDelay: `${index * 200}ms` }}
          className="size-2 rounded-full bg-muted-foreground motion-safe:animate-pulse"
        />
      ))}
      {words.length > 0 ? (
        <span className="ml-1 text-sm text-muted-foreground">{words}</span>
      ) : null}
    </div>
  );
}
