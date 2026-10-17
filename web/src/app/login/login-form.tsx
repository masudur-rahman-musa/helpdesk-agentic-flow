"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/field";
import { logIn } from "@/features/auth/api";
import { SIGN_IN_FAILED_MESSAGE, loginSchema } from "@/lib/schemas";

/**
 * The sign-in form, checked by the same `loginSchema` the route uses so the two
 * never disagree. One message for every failure, so it never tells an attacker
 * which tenant or address exists. The token never reaches this component; it
 * lives in an httpOnly cookie.
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
    const signedIn = await logIn(parsed.data);
    setPending(false);
    if (!signedIn) {
      setFailed(true);
      return;
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
