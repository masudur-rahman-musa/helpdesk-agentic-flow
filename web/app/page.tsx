import { redirect } from "next/navigation";

/** There is no landing page: a signed-in visitor starts at the chat. */
export default function HomePage() {
  redirect("/chat");
}
