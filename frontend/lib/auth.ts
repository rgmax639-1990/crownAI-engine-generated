"use client";

const TOKEN_KEY = "crownai_token";

export function saveToken(token: string) {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    // localStorage unavailable (private browsing, etc.) - session just won't persist.
  }
}

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function clearToken() {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    // ignore
  }
}

const PENDING_CHECKOUT_KEY = "crownai_pending_checkout";

export type PendingCheckout = { leadId: string; plan: string };

// Lets a visitor start the lead-capture -> checkout flow before they've
// signed in: we stash the intent, send them through OAuth, then resume
// checkout automatically once the callback has a token.
export function savePendingCheckout(pending: PendingCheckout) {
  try {
    sessionStorage.setItem(PENDING_CHECKOUT_KEY, JSON.stringify(pending));
  } catch {
    // ignore
  }
}

export function getPendingCheckout(): PendingCheckout | null {
  try {
    const raw = sessionStorage.getItem(PENDING_CHECKOUT_KEY);
    return raw ? (JSON.parse(raw) as PendingCheckout) : null;
  } catch {
    return null;
  }
}

export function clearPendingCheckout() {
  try {
    sessionStorage.removeItem(PENDING_CHECKOUT_KEY);
  } catch {
    // ignore
  }
}
