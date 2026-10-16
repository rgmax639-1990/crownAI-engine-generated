"use client";

import { createContext, ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { Alert, Button, LoadingState, MockBadge, Modal, RadioGroup, TextField } from "./ui";

type Factor = { id: string; factor_type: "passkey" | "totp" | "push" | "sms_otp"; label: string | null; is_primary: boolean };
type Challenge = { challenge_id: string; factor_type: string; delivery?: string; mock?: boolean; mock_code?: string; expires_at: string };

type StepUpCtx = {
  /** Opens the second-factor dialog; resolves with a single-use `mfa_token`, or null if cancelled. */
  requestMfa: (opts?: { reason?: string }) => Promise<string | null>;
};

const Ctx = createContext<StepUpCtx | null>(null);

const FACTOR_LABEL: Record<string, string> = {
  passkey: "Passkey (recommended)",
  totp: "Authenticator app code",
  push: "Push approval on your phone",
  sms_otp: "SMS OTP to registered mobile",
};

export function StepUpProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState<string | undefined>();
  const resolver = useRef<((v: string | null) => void) | null>(null);

  const requestMfa = useCallback((opts?: { reason?: string }) => {
    setReason(opts?.reason);
    setOpen(true);
    return new Promise<string | null>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  const finish = useCallback((token: string | null) => {
    resolver.current?.(token);
    resolver.current = null;
    setOpen(false);
  }, []);

  return (
    <Ctx.Provider value={{ requestMfa }}>
      {children}
      {open && <StepUpDialog reason={reason} onDone={finish} />}
    </Ctx.Provider>
  );
}

export function useStepUp(): StepUpCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useStepUp must be used inside StepUpProvider (portal layout)");
  return c;
}

function StepUpDialog({ reason, onDone }: { reason?: string; onDone: (token: string | null) => void }) {
  const [factors, setFactors] = useState<Factor[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [factor, setFactor] = useState<string>("");
  const [challenge, setChallenge] = useState<Challenge | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    api<{ items: Factor[] }>("/auth/mfa/factors")
      .then((r) => {
        if (!alive) return;
        setFactors(r.items);
        const primary = r.items.find((f) => f.is_primary) ?? r.items[0];
        if (primary) setFactor(primary.factor_type);
      })
      .catch((e) => alive && setLoadError(e instanceof ApiError ? e.message : "Could not load your security factors"));
    return () => {
      alive = false;
    };
  }, []);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const c = await api<Challenge>("/auth/mfa/challenge", { method: "POST", body: { factor_type: factor, purpose: "transaction" } });
      setChallenge(c);
      setCode("");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not start verification");
    } finally {
      setBusy(false);
    }
  };

  const verify = async () => {
    if (!challenge) return;
    if (!/^\d{6}$/.test(code.trim())) {
      setError("Enter the 6-digit code");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ mfa_token: string }>("/auth/mfa/verify", { method: "POST", body: { challenge_id: challenge.challenge_id, response: code.trim() } });
      onDone(r.mfa_token);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Verification failed");
    } finally {
      setBusy(false);
    }
  };

  const unique = factors ? factors.filter((f, i, arr) => arr.findIndex((x) => x.factor_type === f.factor_type) === i) : [];

  return (
    <Modal open onClose={() => onDone(null)} title="Verify it's you" description={reason ?? "This action needs your second factor."} size="sm"
      footer={
        challenge ? (
          <>
            <Button variant="ghost" onClick={() => setChallenge(null)} disabled={busy}>Use another method</Button>
            <Button onClick={verify} loading={busy}>Verify</Button>
          </>
        ) : (
          <>
            <Button variant="ghost" onClick={() => onDone(null)} disabled={busy}>Cancel</Button>
            <Button onClick={start} loading={busy} disabled={!factor}>Continue</Button>
          </>
        )
      }>
      {loadError && <Alert tone="danger">{loadError}</Alert>}
      {!factors && !loadError && <LoadingState rows={2} label="Loading your security factors" />}
      {factors && !challenge && (
        <RadioGroup name="stepup-factor" legend="Choose a verification method" layout="list" value={factor} onChange={setFactor}
          options={unique.map((f) => ({ value: f.factor_type, label: FACTOR_LABEL[f.factor_type] ?? f.factor_type, description: f.label ?? undefined }))} />
      )}
      {challenge && (
        <form onSubmit={(e) => { e.preventDefault(); void verify(); }} className="space-y-3">
          {challenge.delivery && <p className="text-sm text-fg-muted">{challenge.delivery}</p>}
          {challenge.mock && challenge.mock_code && (
            <p className="flex flex-wrap items-center gap-2 text-sm text-fg-muted">
              <MockBadge /> Mock authenticator code: <strong className="font-mono text-fg">{challenge.mock_code}</strong>
            </p>
          )}
          <TextField id="stepup-code" label="Verification code" value={code} onChange={(v) => setCode(v.replace(/\D/g, "").slice(0, 6))}
            inputMode="numeric" autoComplete="one-time-code" maxLength={6} required autoFocus />
        </form>
      )}
      {error && <p role="alert" className="mt-3 text-sm font-medium text-danger">{error}</p>}
    </Modal>
  );
}
