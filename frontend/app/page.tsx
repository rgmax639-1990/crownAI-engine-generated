import { redirect } from "next/navigation";

/** The public site lives under /en and /hi; the root goes to English. */
export default function RootPage() {
  redirect("/en");
}
