import type { Metadata } from "next";
import { ReactNode } from "react";
import { StaffShell } from "@/components/StaffShell";

export const metadata: Metadata = { title: "Staff console", robots: { index: false, follow: false } };

export default function StaffLayout({ children }: { children: ReactNode }) {
  return <StaffShell>{children}</StaffShell>;
}
