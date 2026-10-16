"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { Button, Modal } from "./ui";

/**
 * Mirrors the server's 5-minute idle timeout. Every successful authenticated API call
 * ("auth:activity") resets the timer. 60 seconds before expiry a warning with a live
 * countdown offers "Stay signed in" (POST /auth/session/keepalive) or "Log out now".
 */
export function SessionTimeout({ idleSeconds, warningSeconds, onExpire, onLogout }: {
  idleSeconds: number;
  warningSeconds: number;
  onExpire: () => void;
  onLogout: () => void;
}) {
  const last = useRef<number>(Date.now());
  const [remaining, setRemaining] = useState<number>(idleSeconds);
  const [busy, setBusy] = useState(false);
  const expired = useRef(false);

  useEffect(() => {
    const onActivity = () => {
      last.current = Date.now();
    };
    window.addEventListener("auth:activity", onActivity);
    const timer = setInterval(() => {
      const left = Math.max(0, Math.ceil(idleSeconds - (Date.now() - last.current) / 1000));
      setRemaining(left);
      if (left <= 0 && !expired.current) {
        expired.current = true;
        onExpire();
      }
    }, 1000);
    return () => {
      window.removeEventListener("auth:activity", onActivity);
      clearInterval(timer);
    };
  }, [idleSeconds, onExpire]);

  const stay = useCallback(async () => {
    setBusy(true);
    try {
      await api("/auth/session/keepalive", { method: "POST" });
      last.current = Date.now();
      setRemaining(idleSeconds);
    } catch {
      /* the auth:expired handler takes over */
    } finally {
      setBusy(false);
    }
  }, [idleSeconds]);

  const open = remaining <= warningSeconds && remaining > 0;
  return (
    <Modal open={open} onClose={stay} title="Are you still there?" size="sm"
      footer={
        <>
          <Button variant="secondary" onClick={onLogout}>Log out now</Button>
          <Button onClick={stay} loading={busy}>Stay signed in</Button>
        </>
      }>
      <p className="text-fg">
        For your security you will be signed out in{" "}
        <strong className="tabular-nums" aria-live="polite" aria-atomic="true">{remaining} seconds</strong> because of inactivity.
      </p>
    </Modal>
  );
}
