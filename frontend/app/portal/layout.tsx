import type { Metadata } from "next";
import { ReactNode } from "react";
import { PortalShell } from "@/components/PortalShell";

export const metadata: Metadata = { title: "Internet banking", robots: { index: false, follow: false } };

export default function PortalLayout({ children }: { children: ReactNode }) {
  return <PortalShell>{children}</PortalShell>;
}
