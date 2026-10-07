"use client";

import { useState } from "react";
import { API_BASE } from "@/lib/api";

type Props = {
  label?: string;
  triggerClassName?: string;
  optionsClassName?: string;
  helperText?: string;
};

function GoogleIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true">
      <path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z" />
      <path fill="#FF3D00" d="m6.3 14.7 6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z" />
      <path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z" />
      <path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z" />
    </svg>
  );
}

function MicrosoftIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 23 23" aria-hidden="true">
      <path fill="#F35325" d="M1 1h10v10H1z" />
      <path fill="#81BC06" d="M12 1h10v10H12z" />
      <path fill="#05A6F0" d="M1 12h10v10H1z" />
      <path fill="#FFBA08" d="M12 12h10v10H12z" />
    </svg>
  );
}

// Real <button> that reveals the Google/Microsoft OAuth options on click.
// Used everywhere the app offers a "sign in" action so the trigger is always
// an accessible, clickable button rather than a bare link.
export default function SignInOptions({
  label = "Sign in",
  triggerClassName = "btn-primary",
  optionsClassName = "mt-4 flex flex-col gap-3 sm:flex-row",
  helperText,
}: Props) {
  const [revealed, setRevealed] = useState(false);
  // Leaving for the provider can take a moment on slow networks; show it.
  const [leavingFor, setLeavingFor] = useState<string | null>(null);

  if (!revealed) {
    return (
      <button type="button" className={triggerClassName} onClick={() => setRevealed(true)} aria-expanded={revealed}>
        {label}
      </button>
    );
  }

  return (
    <div>
      {helperText && <p className="body-muted mb-3">{helperText}</p>}
      <div className={optionsClassName}>
        <a
          className="btn-secondary"
          href={`${API_BASE}/auth/google/login`}
          onClick={() => setLeavingFor("Google")}
          aria-disabled={leavingFor !== null}
        >
          <GoogleIcon /> Sign in with Google
        </a>
        <a
          className="btn-secondary"
          href={`${API_BASE}/auth/microsoft/login`}
          onClick={() => setLeavingFor("Microsoft")}
          aria-disabled={leavingFor !== null}
        >
          <MicrosoftIcon /> Sign in with Microsoft
        </a>
      </div>
      <p role="status" aria-live="polite" className="body-muted mt-2 min-h-[1.25rem]">
        {leavingFor ? `Redirecting to ${leavingFor}…` : ""}
      </p>
    </div>
  );
}
