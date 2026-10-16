"use client";

import Link from "next/link";
import { STAFF_NAV, staffCanSee } from "@/components/StaffShell";
import { Card, EmptyState, PageHeader } from "@/components/ui";
import { useSession } from "@/lib/session";

export default function StaffHome() {
  const { session } = useSession();
  if (!session) return null;
  const tiles = STAFF_NAV.filter((n) => n.href !== "/staff" && staffCanSee(session.staff_roles, n));
  return (
    <>
      <PageHeader title={`Welcome, ${session.display_name ?? "colleague"}`}
        description="Open the queues your roles give you access to. Maker and checker duties are always performed by different people." />
      {tiles.length === 0 ? (
        <EmptyState title="No console areas assigned" description="Your account has no staff roles yet. Ask an operations admin to assign them." />
      ) : (
        <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {tiles.map((t) => (
            <li key={t.href}>
              <Link href={t.href} className="block h-full rounded-lg focus-visible:outline-offset-4">
                <Card className="h-full hover:border-primary">
                  <p className="flex items-center gap-2 text-lg font-semibold text-fg">
                    <span aria-hidden="true" className="inline-flex h-9 w-9 items-center justify-center rounded-md bg-primary-soft text-primary">{t.icon}</span>
                    {t.label}
                  </p>
                  <p className="mt-2 text-sm text-fg-muted">{t.description}</p>
                </Card>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
