"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { Alert, Badge, Button, Card, CardHeader, DataView, EmptyState, PageHeader, Tone } from "@/components/ui";
import { useApi } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { isLocale, Locale } from "@/lib/i18n";

type ServiceState = "operational" | "maintenance" | "degraded" | "partial_outage" | "major_outage";
type Overall = "operational" | "degraded" | "partial_outage" | "major_outage";

type Notice = {
  id: string;
  service: string;
  starts_at: string;
  ends_at: string;
  message_en: string;
  message_hi: string;
  state: "upcoming" | "ongoing" | "completed";
  published_at: string;
};

type PublicIncident = {
  id: string;
  summary_en: string;
  summary_hi: string;
  impact: "minor" | "significant" | "major";
  status: "investigating" | "identified" | "monitoring" | "resolved";
  started_at: string;
  resolved_at: string | null;
};

type StatusResponse = {
  overall: Overall;
  overall_label_en: string;
  overall_label_hi: string;
  services: Array<{
    key: string;
    name_en: string;
    name_hi: string;
    state: ServiceState;
    label_en: string;
    label_hi: string;
    since: string | null;
    next_maintenance: Notice | null;
  }>;
  active_incidents: PublicIncident[];
  incident_history: PublicIncident[];
  history_days: number;
  maintenance: Notice[];
  generated_at: string;
};

const STRINGS = {
  en: {
    title: "Service status",
    description: "Live status of our digital banking services. Times are in IST.",
    refresh: "Refresh",
    lastUpdated: "Last updated",
    services: "Services",
    since: "Since",
    nextMaintenance: "Next maintenance",
    maintenance: "Planned maintenance",
    maintenanceDesc: "We announce planned downtime at least 48 hours in advance.",
    noMaintenance: "No planned maintenance",
    noMaintenanceDesc: "There is no planned downtime in the coming days.",
    ongoing: "In progress",
    upcoming: "Scheduled",
    activeIncidents: "Current issues",
    history: "Incident history",
    historyDesc: (d: number) => `Issues in the last ${d} days.`,
    noHistory: "No incidents reported",
    noHistoryDesc: (d: number) => `No service issues in the last ${d} days.`,
    started: "Started",
    resolved: "Resolved",
    error: "We couldn't load the service status",
    service: { website: "Website", internet_banking: "Internet banking", upi: "UPI", cards: "Cards" } as Record<string, string>,
    incidentStatus: { investigating: "Investigating", identified: "Identified", monitoring: "Monitoring", resolved: "Resolved" } as Record<string, string>,
    impact: { minor: "Minor impact", significant: "Significant impact", major: "Major impact" } as Record<string, string>,
    fraud: "To report fraud or block a card, call the 24x7 helpline or use Report fraud at the top of the page.",
  },
  hi: {
    title: "सेवा स्थिति",
    description: "हमारी डिजिटल बैंकिंग सेवाओं की वर्तमान स्थिति। समय IST में है।",
    refresh: "रीफ़्रेश करें",
    lastUpdated: "अंतिम अपडेट",
    services: "सेवाएँ",
    since: "से",
    nextMaintenance: "अगला रखरखाव",
    maintenance: "निर्धारित रखरखाव",
    maintenanceDesc: "निर्धारित डाउनटाइम की सूचना हम कम से कम 48 घंटे पहले देते हैं।",
    noMaintenance: "कोई निर्धारित रखरखाव नहीं",
    noMaintenanceDesc: "आने वाले दिनों में कोई निर्धारित डाउनटाइम नहीं है।",
    ongoing: "जारी है",
    upcoming: "निर्धारित",
    activeIncidents: "वर्तमान समस्याएँ",
    history: "घटनाओं का इतिहास",
    historyDesc: (d: number) => `पिछले ${d} दिनों की समस्याएँ।`,
    noHistory: "कोई घटना दर्ज नहीं",
    noHistoryDesc: (d: number) => `पिछले ${d} दिनों में कोई सेवा समस्या नहीं।`,
    started: "शुरू",
    resolved: "समाधान",
    error: "सेवा स्थिति लोड नहीं हो सकी",
    service: { website: "वेबसाइट", internet_banking: "इंटरनेट बैंकिंग", upi: "UPI", cards: "कार्ड" } as Record<string, string>,
    incidentStatus: { investigating: "जाँच जारी", identified: "कारण पहचाना गया", monitoring: "निगरानी में", resolved: "समाधान हो गया" } as Record<string, string>,
    impact: { minor: "मामूली प्रभाव", significant: "महत्वपूर्ण प्रभाव", major: "बड़ा प्रभाव" } as Record<string, string>,
    fraud: "धोखाधड़ी की रिपोर्ट करने या कार्ड ब्लॉक करने के लिए 24x7 हेल्पलाइन पर कॉल करें या पेज के ऊपर 'धोखाधड़ी रिपोर्ट करें' का उपयोग करें।",
  },
};

const STATE_TONE: Record<ServiceState, Tone> = {
  operational: "success",
  maintenance: "info",
  degraded: "warning",
  partial_outage: "danger",
  major_outage: "danger",
};
const STATE_ICON: Record<ServiceState, string> = {
  operational: "✓",
  maintenance: "◷",
  degraded: "!",
  partial_outage: "✕",
  major_outage: "✕",
};
const OVERALL_ALERT: Record<Overall, "success" | "warning" | "danger"> = {
  operational: "success",
  degraded: "warning",
  partial_outage: "danger",
  major_outage: "danger",
};

export default function StatusPage() {
  const params = useParams<{ locale: string }>();
  const rawLocale = params?.locale;
  const locale: Locale = isLocale(rawLocale) ? rawLocale : "en";
  const s = STRINGS[locale];
  const state = useApi<StatusResponse>("/status", { auth: false });
  const [refreshedAt, setRefreshedAt] = useState<string | null>(null);

  useEffect(() => {
    if (state.data) setRefreshedAt(new Date().toISOString());
  }, [state.data]);

  // Re-check every 60 seconds while the page is open (the API is cacheable for 60 s).
  const { reload } = state;
  useEffect(() => {
    const t = setInterval(reload, 60000);
    return () => clearInterval(t);
  }, [reload]);

  const pick = (en: string, hi: string) => (locale === "hi" ? hi : en);

  return (
    <>
      <PageHeader
        title={s.title}
        description={s.description}
        actions={
          <Button variant="secondary" size="sm" onClick={state.reload} loading={state.loading && state.data !== null}>
            {s.refresh}
          </Button>
        }
      />
      <DataView state={state} errorTitle={s.error} loadingRows={5}>
        {(d) => (
          <div className="space-y-6">
            <Alert tone={OVERALL_ALERT[d.overall]} title={pick(d.overall_label_en, d.overall_label_hi)}>
              {s.lastUpdated}: {formatDateTime(d.generated_at || refreshedAt)}
            </Alert>

            {d.active_incidents.length > 0 && (
              <Card as="section">
                <CardHeader title={s.activeIncidents} />
                <ul className="space-y-3">
                  {d.active_incidents.map((i) => (
                    <li key={i.id} className="rounded-md border border-border p-3">
                      <p className="font-medium text-fg">{pick(i.summary_en, i.summary_hi)}</p>
                      <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-fg-muted">
                        <Badge tone="warning">{s.incidentStatus[i.status] ?? i.status}</Badge>
                        <span>{s.impact[i.impact]}</span>
                        <span>· {s.started}: {formatDateTime(i.started_at)}</span>
                      </p>
                    </li>
                  ))}
                </ul>
                <p className="mt-3 text-sm text-fg-muted">{s.fraud}</p>
              </Card>
            )}

            <Card as="section">
              <CardHeader title={s.services} />
              <ul className="divide-y divide-border">
                {d.services.map((svc) => (
                  <li key={svc.key} className="flex flex-wrap items-center justify-between gap-3 py-3">
                    <div className="min-w-0">
                      <p className="font-medium text-fg">{pick(svc.name_en, svc.name_hi)}</p>
                      <p className="text-xs text-fg-muted">
                        {svc.since && svc.state !== "operational" ? `${s.since} ${formatDateTime(svc.since)}` : null}
                        {svc.next_maintenance && (
                          <span className="block">
                            {s.nextMaintenance}: {formatDateTime(svc.next_maintenance.starts_at)} – {formatDateTime(svc.next_maintenance.ends_at)}
                          </span>
                        )}
                      </p>
                    </div>
                    <Badge tone={STATE_TONE[svc.state]}>
                      <span aria-hidden="true">{STATE_ICON[svc.state]}</span> {pick(svc.label_en, svc.label_hi)}
                    </Badge>
                  </li>
                ))}
              </ul>
            </Card>

            <Card as="section">
              <CardHeader title={s.maintenance} description={s.maintenanceDesc} />
              {d.maintenance.length === 0 ? (
                <EmptyState title={s.noMaintenance} description={s.noMaintenanceDesc} icon="◷" />
              ) : (
                <ul className="space-y-3">
                  {d.maintenance.map((n) => (
                    <li key={n.id} className="rounded-md border border-border p-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium text-fg">{s.service[n.service] ?? n.service}</span>
                        <Badge tone={n.state === "ongoing" ? "warning" : "info"}>{n.state === "ongoing" ? s.ongoing : s.upcoming}</Badge>
                      </div>
                      <p className="mt-1 text-sm tabular-nums text-fg">
                        {formatDateTime(n.starts_at)} – {formatDateTime(n.ends_at)}
                      </p>
                      <p className="mt-1 text-sm text-fg-muted" lang={locale}>{pick(n.message_en, n.message_hi)}</p>
                    </li>
                  ))}
                </ul>
              )}
            </Card>

            <Card as="section">
              <CardHeader title={s.history} description={s.historyDesc(d.history_days)} />
              {d.incident_history.length === 0 ? (
                <EmptyState title={s.noHistory} description={s.noHistoryDesc(d.history_days)} icon="✓" />
              ) : (
                <ul className="divide-y divide-border">
                  {d.incident_history.map((i) => (
                    <li key={i.id} className="py-3">
                      <p className="font-medium text-fg">{pick(i.summary_en, i.summary_hi)}</p>
                      <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-fg-muted">
                        <Badge tone={i.status === "resolved" ? "success" : "warning"}>{s.incidentStatus[i.status] ?? i.status}</Badge>
                        <span>{s.started}: {formatDateTime(i.started_at)}</span>
                        {i.resolved_at && <span>· {s.resolved}: {formatDateTime(i.resolved_at)}</span>}
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </div>
        )}
      </DataView>
    </>
  );
}
