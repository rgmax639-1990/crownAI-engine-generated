"use client";

import { FormEvent, useState } from "react";
import {
  Alert, Badge, Button, Card, CardHeader, DataTable, DataView, EmptyState, PageHeader, SelectField, TextAreaField, TextField, Tone,
} from "@/components/ui";
import { api, ApiError, useApi } from "@/lib/api";
import { formatDateTime, humanize } from "@/lib/format";

type Notice = {
  id: string;
  service: string;
  starts_at: string;
  ends_at: string;
  message_en: string;
  message_hi: string;
  created_by_name: string | null;
  created_at: string;
  state: "upcoming" | "ongoing" | "completed";
  notice_hours: number;
  meets_notice_rule: boolean;
};

type NoticesResponse = { items: Notice[]; minimum_notice_hours: number; max_window_hours: number; services: string[] };

const SERVICE_LABEL: Record<string, string> = { website: "Website", internet_banking: "Internet banking", upi: "UPI", cards: "Cards" };
const STATE_TONE: Record<string, Tone> = { upcoming: "info", ongoing: "warning", completed: "neutral" };

/** IST wall-clock datetime-local value, `hours` from now. */
function istLocal(hoursAhead = 0): string {
  return new Date(Date.now() + hoursAhead * 3600000 + 330 * 60000).toISOString().slice(0, 16);
}

/** Interpret a datetime-local value as IST and return epoch ms. */
function istMs(local: string): number {
  return Date.parse(`${local}:00+05:30`);
}

export default function MaintenancePage() {
  const state = useApi<NoticesResponse>("/admin/maintenance-notices");
  const [notice, setNotice] = useState<string | null>(null);
  return (
    <>
      <PageHeader title="Maintenance notices" eyebrow="Planned downtime"
        description="Publish planned downtime in English and Hindi. Notices must be published at least 48 hours before the window starts; they appear on the public status page." />
      {notice && <Alert tone="success" className="mb-4" action={<Button size="sm" variant="ghost" onClick={() => setNotice(null)}>Dismiss</Button>}>{notice}</Alert>}
      <NoticeForm minHours={state.data?.minimum_notice_hours ?? 48} maxWindow={state.data?.max_window_hours ?? 24}
        onCreated={(n) => { setNotice(`Notice published for ${SERVICE_LABEL[n.service] ?? n.service}: ${formatDateTime(n.starts_at)} – ${formatDateTime(n.ends_at)}.`); state.reload(); }} />
      <Card padded={false} as="section">
        <div className="p-4 sm:p-5"><CardHeader title="All notices" description="Upcoming, ongoing and past windows." className="mb-0" /></div>
        <DataView state={state} isEmpty={(d) => d.items.length === 0} empty={<div className="p-4"><EmptyState title="No maintenance notices yet" /></div>}>
          {(d) => (
            <DataTable<Notice>
              caption="Maintenance notices"
              rows={d.items}
              rowKey={(n) => n.id}
              columns={[
                { key: "service", header: "Service", render: (n) => SERVICE_LABEL[n.service] ?? n.service },
                {
                  key: "window", header: "Window (IST)",
                  render: (n) => <div className="whitespace-nowrap text-xs tabular-nums"><p>{formatDateTime(n.starts_at)}</p><p>to {formatDateTime(n.ends_at)}</p></div>,
                },
                { key: "state", header: "State", render: (n) => <Badge tone={STATE_TONE[n.state]}>{humanize(n.state)}</Badge> },
                {
                  key: "messages", header: "Messages",
                  render: (n) => (
                    <div className="max-w-md space-y-1 text-xs">
                      <p><span className="font-semibold">EN</span> {n.message_en}</p>
                      <p lang="hi"><span className="font-semibold">HI</span> {n.message_hi}</p>
                    </div>
                  ),
                },
                {
                  key: "published", header: "Published",
                  render: (n) => (
                    <div className="text-xs">
                      <p>{formatDateTime(n.created_at)}</p>
                      <p className="text-fg-muted">{n.created_by_name ?? "—"} · {n.notice_hours} h ahead</p>
                      {!n.meets_notice_rule && <Badge tone="warning">Under {d.minimum_notice_hours} h: hidden from public page</Badge>}
                    </div>
                  ),
                },
              ]}
            />
          )}
        </DataView>
      </Card>
    </>
  );
}

function NoticeForm({ minHours, maxWindow, onCreated }: { minHours: number; maxWindow: number; onCreated: (n: Notice) => void }) {
  const initial = { service: "", starts_at: "", ends_at: "", message_en: "", message_hi: "" };
  const [v, setV] = useState(initial);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [touched, setTouched] = useState<Record<string, boolean>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const validate = (x: typeof v) => {
    const e: Record<string, string> = {};
    if (!x.service) e.service = "Choose the affected service";
    if (!x.starts_at) e.starts_at = "Enter the start time";
    else if (istMs(x.starts_at) < Date.now() + minHours * 3600000)
      e.starts_at = `Must start at least ${minHours} hours from now (earliest ${istLocal(minHours).replace("T", " ")} IST)`;
    if (!x.ends_at) e.ends_at = "Enter the end time";
    else if (x.starts_at && istMs(x.ends_at) <= istMs(x.starts_at)) e.ends_at = "End time must be after the start time";
    else if (x.starts_at && istMs(x.ends_at) - istMs(x.starts_at) > maxWindow * 3600000) e.ends_at = `The window can be at most ${maxWindow} hours`;
    if (x.message_en.trim().length < 10) e.message_en = "Enter the English message (at least 10 characters)";
    if (x.message_hi.trim().length < 10) e.message_hi = "Enter the Hindi message (at least 10 characters)";
    return e;
  };

  const live = validate(v);
  const shown = (k: string) => errors[k] || (touched[k] && v[k as keyof typeof v] ? live[k] : undefined);
  const set = (k: keyof typeof v) => (val: string) => {
    setV((x) => ({ ...x, [k]: val }));
    setTouched((t) => ({ ...t, [k]: true }));
    setErrors((e) => ({ ...e, [k]: "" }));
  };

  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    const e = validate(v);
    setErrors(e);
    setFormError(null);
    const first = Object.keys(e)[0];
    if (first) {
      document.getElementById(first)?.focus();
      return;
    }
    setBusy(true);
    try {
      const n = await api<Notice>("/admin/maintenance-notices", { method: "POST", body: v });
      setV(initial);
      setTouched({});
      onCreated(n);
    } catch (err) {
      if (err instanceof ApiError) {
        setErrors(err.fieldErrors);
        setFormError(err.message);
      } else setFormError("Something went wrong. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="mb-6" as="section">
      <CardHeader title="Publish a notice" description={`Times are IST. Minimum notice ${minHours} hours; maximum window ${maxWindow} hours.`} />
      <form onSubmit={submit} noValidate className="space-y-4" aria-label="Publish maintenance notice">
        {formError && <Alert tone="danger">{formError}</Alert>}
        <div className="grid gap-4 sm:grid-cols-3">
          <SelectField id="service" label="Service" required value={v.service} onChange={set("service")} placeholder="Choose"
            error={shown("service")} options={Object.entries(SERVICE_LABEL).map(([value, label]) => ({ value, label }))} />
          <TextField id="starts_at" label="Starts (IST)" type="datetime-local" required value={v.starts_at} min={istLocal(minHours)}
            onChange={set("starts_at")} error={shown("starts_at")} />
          <TextField id="ends_at" label="Ends (IST)" type="datetime-local" required value={v.ends_at} min={v.starts_at || istLocal(minHours)}
            onChange={set("ends_at")} error={shown("ends_at")} />
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <TextAreaField id="message_en" label="Message (English)" required value={v.message_en} onChange={set("message_en")} rows={3}
            maxLength={500} error={shown("message_en")} placeholder="Internet banking will be unavailable for scheduled maintenance." />
          <TextAreaField id="message_hi" label="Message (Hindi)" required value={v.message_hi} onChange={set("message_hi")} rows={3}
            maxLength={500} error={shown("message_hi")} placeholder="निर्धारित रखरखाव के कारण इंटरनेट बैंकिंग उपलब्ध नहीं रहेगी।" />
        </div>
        <Button type="submit" loading={busy}>Publish notice</Button>
      </form>
    </Card>
  );
}
