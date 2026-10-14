"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/field";
import { SIGN_IN_FAILED_MESSAGE, loginSchema } from "@/lib/schemas";

/**
 * The three fields, pre-checked by the SAME `loginSchema` the route handler
 * runs, so the form's message and the
 * server's refusal cannot disagree about what is acceptable.
 *
 * One generic message for every failure, whether the pre-check refused the
 * input or agent-api answered 401: naming the field that was wrong tells an
 * attacker which tenant slugs and addresses exist, which is exactly what
 * `/auth/login`'s single typed 401 is built to avoid.
 *
 * The bearer never reaches this component. `POST /api/login` answers
 * `{ ok: true }` and sets an httpOnly cookie, so there is nothing here to put
 * in `localStorage` even by accident (`tests/login-route.test.ts`, "never
 * returns the bearer in the response body").
 */
export function LoginForm() {
  const router = useRouter();
  const [failed, setFailed] = useState(false);
  const [pending, setPending] = useState(false);

  async function submit(form: FormData): Promise<void> {
    setFailed(false);

    const candidate = {
      tenant_slug: String(form.get("tenant_slug") ?? ""),
      email: String(form.get("email") ?? ""),
      password: String(form.get("password") ?? ""),
    };

    const parsed = loginSchema.safeParse(candidate);
    if (!parsed.success) {
      setFailed(true);
      return;
    }

    setPending(true);
    try {
      const response = await fetch("/api/login", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(parsed.data),
      });
      if (!response.ok) {
        setFailed(true);
        return;
      }
    } catch {
      setFailed(true);
      return;
    } finally {
      setPending(false);
    }

    router.replace("/chat");
    router.refresh();
  }

  return (
    <Card className="mt-4 p-6">
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          void submit(new FormData(event.currentTarget));
        }}
      >
        <div className="space-y-1">
          <Label htmlFor="tenant_slug">Tenant</Label>
          <Input id="tenant_slug" name="tenant_slug" autoComplete="organization" required />
        </div>
        <div className="space-y-1">
          <Label htmlFor="email">Email</Label>
          <Input id="email" name="email" type="email" autoComplete="username" required />
        </div>
        <div className="space-y-1">
          <Label htmlFor="password">Password</Label>
          <Input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
          />
        </div>

        {failed ? (
          <p role="alert" className="text-sm text-destructive">
            {SIGN_IN_FAILED_MESSAGE}
          </p>
        ) : null}

        <Button type="submit" disabled={pending}>
          {pending ? "Signing in" : "Sign in"}
        </Button>
      </form>
    </Card>
  );
}
