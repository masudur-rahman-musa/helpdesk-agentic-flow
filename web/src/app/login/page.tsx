import { AppShell } from "@/components/layout/app-shell";
import { LoginForm } from "@/app/login/login-form";

export default function LoginPage() {
  return (
    <AppShell showNav={false}>
      <div className="mx-auto max-w-sm">
        <h1 className="text-lg font-semibold">Sign in</h1>
        <LoginForm />
      </div>
    </AppShell>
  );
}
