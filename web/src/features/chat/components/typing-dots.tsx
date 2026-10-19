"use client";

/**
 * The bubble shown while the assistant works and no reply text has arrived.
 * Three dots pulse a little apart; `motion-safe:` keeps them still for anyone
 * who asked their system for less motion. The text is for screen readers only.
 */
export function TypingDots() {
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
    </div>
  );
}
