"use client";

import { ReactNode, useState } from "react";
import {
  Alert, Badge, Button, Card, DataTable, DataView, MockBadge, Modal, PageHeader, SelectField, StatTile, TextAreaField, Tone,
} from "@/components/ui";
import { api, ApiError, useApi } from "@/lib/api";
import { formatDateTime, humanize } from "@/lib/format";
import { useSession } from "@/lib/session";

type Approval = {
  id: string;
  adapter_id: string;
  status: "pending" | "approved" | "rejected";
  evidence_refs: string;
  justification: string;
  requested_by: string;
  requested_by_name: string | null;
  requested_at: string;
  decided_by: string | null;
  decided_by_name: string | null;
  decided_at: string | null;
  decision_comment: string | null;
  used_to_enable_live: boolean;
};

type Adapter = {
  id: string;
  provider: string;
  label: string;
  environment: string;
  mode: "mock" | "live_requested" | "live_approved" | "live";
  badge: "MOCK" | "LIVE";
  health_status: string;
  last_health_check: string | null;
  env_vars: Array<{ name: string; set: boolean }>;
  env_vars_set: number;
  env_vars_total: number;
  credentials_complete: boolean;
  active_approval_id: string | null;
  latest_approval: Approval | null;
  can_enable_live: boolean;
};

type IntegrationsResponse = {
  environment: string;
  current_environment: string;
  environments: string[];
  adapters: Adapter[];
  live_count: number;
  pending_count: number;
  credentials_note: string;
};

type ActionKind = "request" | "decide" | "enable" | "revert";

const APPROVAL_TONE: Record<string, Tone> = { pending: "warning", approved: "success", rejected: "danger" };

function healthTone(h: string): Tone {
  return h === "healthy" ? "success" : h === "degraded" ? "warning" : "danger";
}

export default function IntegrationsPage() {
  const { session, hasRole } = useSession();
  const [env, setEnv] = useState<string>("");
  const state = useApi<IntegrationsResponse>(`/admin/integrations${env ? `?environment=${encodeURIComponent(env)}` : ""}`);
  const [action, setAction] = useState<{ kind: ActionKind; adapter: Adapter } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const isOps = hasRole("ops_admin");
  const isApprover = hasRole("integration_approver");

  const done = (msg: string) => {
    setAction(null);
    setNotice(msg);
    state.reload();
  };

  return (
    <>
      <PageHeader
        title="Integrations"
        eyebrow="Mock / live adapters"
        description="Every external institution runs on a MOCK adapter until a live request is approved by a different integration approver and the credentials are configured. Credential values are never shown."
      />
      {notice && (
        <Alert tone="success" className="mb-4" action={<Button size="sm" variant="ghost" onClick={() => setNotice(null)}>Dismiss</Button>}>
          {notice}
        </Alert>
      )}
      <Card className="mb-4">
        <div className="grid gap-4 sm:grid-cols-[minmax(0,16rem)_1fr] sm:items-end">
          <SelectField id="environment" label="Environment" value={env || state.data?.environment || ""}
            onChange={(v) => setEnv(v)}
            options={(state.data?.environments ?? ["dev", "qa", "uat", "production"]).map((e) => ({
              value: e, label: `${e.toUpperCase()}${state.data?.current_environment === e ? " (this server)" : ""}`,
            }))} />
          {state.data && (
            <div className="grid gap-3 sm:grid-cols-3">
              <StatTile label="Adapters" value={state.data.adapters.length} />
              <StatTile label="Live" value={state.data.live_count} tone={state.data.live_count ? "warning" : "neutral"} />
              <StatTile label="Pending approvals" value={state.data.pending_count} tone={state.data.pending_count ? "info" : "neutral"} />
            </div>
          )}
        </div>
      </Card>

      <DataView state={state} isEmpty={(d) => d.adapters.length === 0} loadingRows={8}>
        {(d) => (
          <Card padded={false}>
            <DataTable<Adapter>
              caption={`Integration adapters in ${d.environment}`}
              rows={d.adapters}
              rowKey={(a) => a.id}
              columns={[
                {
                  key: "provider", header: "Provider",
                  render: (a) => (
                    <div className="min-w-[12rem]">
                      <p className="font-medium">{a.label}</p>
                      <p className="text-xs text-fg-muted">{a.provider}</p>
                    </div>
                  ),
                },
                {
                  key: "mode", header: "Mode",
                  render: (a) => (
                    <div className="flex flex-col items-start gap-1">
                      {a.badge === "LIVE" ? <Badge tone="danger">LIVE</Badge> : <MockBadge />}
                      {a.mode !== "mock" && a.mode !== "live" && <span className="text-xs text-fg-muted">{humanize(a.mode)}</span>}
                    </div>
                  ),
                },
                {
                  key: "approval", header: "Approval",
                  render: (a) => a.latest_approval ? (
                    <div className="text-xs">
                      <Badge tone={APPROVAL_TONE[a.latest_approval.status]}>{humanize(a.latest_approval.status)}</Badge>
                      <p className="mt-1 text-fg-muted">Requested by {a.latest_approval.requested_by_name ?? "—"}</p>
                      {a.latest_approval.decided_by_name && (
                        <p className="text-fg-muted">Decided by {a.latest_approval.decided_by_name}</p>
                      )}
                    </div>
                  ) : <span className="text-fg-muted">—</span>,
                },
                {
                  key: "health", header: "Health",
                  render: (a) => (
                    <div className="text-xs">
                      <Badge tone={healthTone(a.health_status)}>{humanize(a.health_status)}</Badge>
                      <p className="mt-1 text-fg-muted">{formatDateTime(a.last_health_check)}</p>
                    </div>
                  ),
                },
                {
                  key: "env", header: "Credentials",
                  render: (a) => (
                    <details className="text-xs">
                      <summary className="cursor-pointer">
                        <Badge tone={a.credentials_complete ? "success" : "neutral"}>{a.env_vars_set}/{a.env_vars_total} set</Badge>
                      </summary>
                      <ul className="mt-1 space-y-0.5">
                        {a.env_vars.map((v) => (
                          <li key={v.name} className="font-mono">
                            <span aria-hidden="true">{v.set ? "✓" : "✕"}</span> {v.name}
                            <span className="sr-only">{v.set ? " is set" : " is not set"}</span>
                          </li>
                        ))}
                      </ul>
                    </details>
                  ),
                },
                {
                  key: "actions", header: "Actions",
                  render: (a) => {
                    const pending = a.latest_approval?.status === "pending" ? a.latest_approval : null;
                    const own = !!pending && pending.requested_by === session?.user_id;
                    return (
                      <div className="flex min-w-[10rem] flex-col items-start gap-2">
                        {isOps && a.mode === "mock" && (
                          <Button size="sm" variant="outline" onClick={() => setAction({ kind: "request", adapter: a })}>Request live</Button>
                        )}
                        {isApprover && pending && (
                          <>
                            <Button size="sm" onClick={() => setAction({ kind: "decide", adapter: a })} disabled={own}
                              aria-describedby={own ? `own-${a.id}` : undefined}>
                              Approve / reject
                            </Button>
                            {own && <p id={`own-${a.id}`} className="text-xs text-fg-muted">You requested this, so another approver must decide.</p>}
                          </>
                        )}
                        {(isOps || isApprover) && a.can_enable_live && (
                          <Button size="sm" variant="danger" onClick={() => setAction({ kind: "enable", adapter: a })}>Enable live</Button>
                        )}
                        {(isOps || isApprover) && a.mode !== "mock" && (
                          <Button size="sm" variant="secondary" onClick={() => setAction({ kind: "revert", adapter: a })}>Revert to mock</Button>
                        )}
                      </div>
                    );
                  },
                },
              ]}
            />
            <p className="border-t border-border p-4 text-xs text-fg-muted">{d.credentials_note}</p>
          </Card>
        )}
      </DataView>

      {action && (
        <ActionModal key={`${action.kind}-${action.adapter.id}`} kind={action.kind} adapter={action.adapter}
          onClose={() => setAction(null)} onDone={done} />
      )}
    </>
  );
}

function ActionModal({ kind, adapter, onClose, onDone }: {
  kind: ActionKind; adapter: Adapter; onClose: () => void; onDone: (msg: string) => void;
}) {
  const [evidence, setEvidence] = useState("");
  const [justification, setJustification] = useState("");
  const [comment, setComment] = useState("");
  const [reason, setReason] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [missing, setMissing] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const approval = adapter.latest_approval;

  const run = async (fn: () => Promise<string>, local: Record<string, string> = {}) => {
    setErrors(local);
    setFormError(null);
    setMissing([]);
    if (Object.keys(local).length) return;
    setBusy(true);
    try {
      onDone(await fn());
    } catch (e) {
      if (e instanceof ApiError) {
        setErrors(e.fieldErrors);
        setFormError(e.message);
        const det = (e.details as { detail?: { missing_env_vars?: string[] } } | null)?.detail;
        if (det?.missing_env_vars) setMissing(det.missing_env_vars);
      } else {
        setFormError("Something went wrong. Please try again.");
      }
    } finally {
      setBusy(false);
    }
  };

  const post = (path: string, body: unknown) => api(path, { method: "POST", body });

  const titles: Record<ActionKind, string> = {
    request: "Request live mode",
    decide: "Decide live request",
    enable: "Enable live mode",
    revert: "Revert to mock",
  };

  let content: ReactNode = null;
  let submit: () => void = () => undefined;
  let submitLabel = "Submit";
  let footerExtra: ReactNode = null;

  if (kind === "request") {
    submitLabel = "Submit request";
    submit = () => run(async () => {
      await post(`/admin/integrations/${adapter.id}/live-requests`, { evidence_refs: evidence, justification });
      return `Live request for ${adapter.label} (${adapter.environment}) sent for approval.`;
    }, {
      ...(evidence.trim().length < 5 ? { evidence_refs: "List the evidence references (at least 5 characters)" } : {}),
      ...(justification.trim().length < 20 ? { justification: "Explain why live mode is needed (at least 20 characters)" } : {}),
    });
    content = (
      <div className="space-y-4">
        <TextAreaField id="evidence_refs" label="Evidence references" required value={evidence} onChange={setEvidence}
          error={errors.evidence_refs} rows={3} maxLength={2000}
          placeholder="e.g. Certification report ID, UAT sign-off, InfoSec review reference" />
        <TextAreaField id="justification" label="Justification" required value={justification} onChange={setJustification}
          error={errors.justification} rows={4} maxLength={4000} />
        <p className="text-sm text-fg-muted">A different integration approver must approve this request. Live mode also needs all credential variables to be set on the server.</p>
      </div>
    );
  } else if (kind === "decide" && approval) {
    const decide = (decision: "approved" | "rejected") => run(async () => {
      await post(`/admin/integrations/approvals/${approval.id}/decide`, { decision, comment });
      return `Live request for ${adapter.label} ${decision}.`;
    }, decision === "rejected" && comment.trim().length < 5 ? { comment: "Give a reason for the rejection (at least 5 characters)" } : {});
    submitLabel = "Approve";
    submit = () => decide("approved");
    footerExtra = <Button variant="danger" onClick={() => decide("rejected")} loading={busy}>Reject</Button>;
    content = (
      <div className="space-y-4">
        <dl className="space-y-2 text-sm">
          <div><dt className="font-medium">Requested by</dt><dd className="text-fg-muted">{approval.requested_by_name} · {formatDateTime(approval.requested_at)}</dd></div>
          <div><dt className="font-medium">Evidence</dt><dd className="whitespace-pre-wrap text-fg-muted">{approval.evidence_refs}</dd></div>
          <div><dt className="font-medium">Justification</dt><dd className="whitespace-pre-wrap text-fg-muted">{approval.justification}</dd></div>
        </dl>
        <TextAreaField id="comment" label="Comment" hint="Required when rejecting" value={comment} onChange={setComment}
          error={errors.comment} rows={3} maxLength={2000} />
      </div>
    );
  } else if (kind === "enable") {
    submitLabel = "Enable live";
    submit = () => run(async () => {
      await post(`/admin/integrations/${adapter.id}/enable-live`, { approval_id: adapter.active_approval_id });
      return `${adapter.label} is now LIVE in ${adapter.environment}.`;
    });
    content = (
      <div className="space-y-3 text-sm">
        <Alert tone="warning" title="Real institution calls">
          Live mode sends requests to {adapter.label}. It uses approval {adapter.active_approval_id?.slice(0, 8)}… approved by {approval?.decided_by_name ?? "—"}.
        </Alert>
        <p>Credential variables set: {adapter.env_vars_set}/{adapter.env_vars_total}</p>
        {missing.length > 0 && (
          <Alert tone="danger" title="Missing credential variables">
            <ul className="list-disc pl-5 font-mono">
              {missing.map((m) => <li key={m}>{m}</li>)}
            </ul>
          </Alert>
        )}
      </div>
    );
  } else if (kind === "revert") {
    submitLabel = "Revert to mock";
    submit = () => run(async () => {
      await post(`/admin/integrations/${adapter.id}/revert-to-mock`, { reason });
      return `${adapter.label} reverted to MOCK in ${adapter.environment}.`;
    }, reason.trim().length < 5 ? { reason: "Give a reason (at least 5 characters)" } : {});
    content = (
      <div className="space-y-3">
        {adapter.mode === "live_requested" && <p className="text-sm text-fg-muted">The pending live request will be withdrawn.</p>}
        <TextAreaField id="reason" label="Reason" required value={reason} onChange={setReason} error={errors.reason} rows={3} maxLength={1000} />
      </div>
    );
  }

  return (
    <Modal open onClose={onClose} title={titles[kind]} description={`${adapter.label} · ${adapter.environment.toUpperCase()}`}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          {footerExtra}
          <Button variant={kind === "enable" ? "danger" : "primary"} onClick={submit} loading={busy}>{submitLabel}</Button>
        </>
      }>
      {formError && missing.length === 0 && <Alert tone="danger" className="mb-4">{formError}</Alert>}
      {content}
    </Modal>
  );
}
