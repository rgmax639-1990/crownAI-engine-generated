import { FormEvent, useCallback, useRef, useState } from "react";
import { ApiError } from "./api";

export type Errors<V> = Partial<Record<keyof V & string, string>>;

/**
 * Small form state helper.
 *  - A field counts as touched as soon as it changes (and on blur), so messages show while typing.
 *  - All messages show once a submit was attempted.
 *  - The submit button stays enabled while the form is invalid; it is disabled only while saving.
 *  - Server 422 field errors are mapped back onto fields; other errors go to `formError`.
 */
export function useForm<V extends Record<string, unknown>>(opts: { initial: V; validate?: (v: V) => Errors<V> }) {
  const { initial, validate } = opts;
  const [values, setValues] = useState<V>(initial);
  const [touched, setTouched] = useState<Partial<Record<keyof V & string, boolean>>>({});
  const [submitAttempted, setSubmitAttempted] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [serverErrors, setServerErrors] = useState<Errors<V>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const busy = useRef(false);

  const allErrors: Errors<V> = { ...(validate ? validate(values) : {}), ...serverErrors };

  const errors: Errors<V> = {};
  for (const k of Object.keys(allErrors) as (keyof V & string)[]) {
    const has = values[k] !== "" && values[k] !== null && values[k] !== undefined && values[k] !== false;
    if (submitAttempted || touched[k] || has || serverErrors[k]) errors[k] = allErrors[k];
  }

  const set = useCallback(<K extends keyof V & string>(key: K, value: V[K]) => {
    setValues((v) => ({ ...v, [key]: value }));
    setTouched((t) => ({ ...t, [key]: true }));
    setServerErrors((e) => {
      if (!e[key]) return e;
      const n = { ...e };
      delete n[key];
      return n;
    });
  }, []);

  const blur = useCallback(<K extends keyof V & string>(key: K) => setTouched((t) => ({ ...t, [key]: true })), []);

  const reset = useCallback(
    (next?: V) => {
      setValues(next ?? initial);
      setTouched({});
      setSubmitAttempted(false);
      setServerErrors({});
      setFormError(null);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  /** Wrap a submit handler: validates, focuses the first invalid field (by element id = field name), handles API errors. */
  const handleSubmit = (fn: (v: V) => Promise<void> | void) => async (e?: FormEvent) => {
    e?.preventDefault();
    if (busy.current) return;
    setSubmitAttempted(true);
    setFormError(null);
    const errs = validate ? validate(values) : {};
    const firstKey = Object.keys(errs).find((k) => errs[k as keyof V & string]);
    if (firstKey) {
      if (typeof document !== "undefined") document.getElementById(firstKey)?.focus();
      return;
    }
    busy.current = true;
    setSubmitting(true);
    try {
      await fn(values);
    } catch (err) {
      if (err instanceof ApiError) {
        const fe = err.fieldErrors || {};
        const known: Errors<V> = {};
        let unmatched = false;
        for (const [k, msg] of Object.entries(fe)) {
          if (k in values) known[k as keyof V & string] = msg;
          else unmatched = true;
        }
        setServerErrors(known);
        if (!Object.keys(known).length || unmatched) setFormError(err.message);
        const fk = Object.keys(known)[0];
        if (fk && typeof document !== "undefined") document.getElementById(fk)?.focus();
      } else {
        setFormError("Something went wrong. Please try again.");
      }
    } finally {
      busy.current = false;
      setSubmitting(false);
    }
  };

  return {
    values,
    setValues,
    set,
    blur,
    errors,
    allErrors,
    isValid: Object.keys(allErrors).length === 0,
    submitting,
    submitAttempted,
    formError,
    setFormError,
    setServerErrors,
    reset,
    handleSubmit,
  };
}

// Common validators ---------------------------------------------------------
export const isEmail = (v: string) => /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v.trim());
export const isIndianMobile = (v: string) => /^(\+?91[\s-]?)?[6-9]\d{9}$/.test(v.replace(/[\s-]/g, ""));
export const isMobile = (v: string) => isIndianMobile(v) || /^\+[1-9]\d{7,14}$/.test(v.replace(/[\s-]/g, ""));
export const isPin = (v: string) => /^[1-9]\d{5}$/.test(v.trim());
export const isPan = (v: string) => /^[A-Z]{5}\d{4}[A-Z]$/.test(v.trim().toUpperCase());
export const isIfsc = (v: string) => /^[A-Z]{4}0[A-Z0-9]{6}$/.test(v.trim().toUpperCase());
export const isOtp = (v: string) => /^\d{6}$/.test(v.trim());
export const isVpa = (v: string) => /^[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z][a-zA-Z0-9]{1,64}$/.test(v.trim());
