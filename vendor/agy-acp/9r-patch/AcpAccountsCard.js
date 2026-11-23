"use client";

import { useCallback, useEffect, useState } from "react";
import { Card, Button, Badge, Input, Modal } from "@/shared/components";
import { useNotificationStore } from "@/store/notificationStore";

/**
 * Accounts card for the Antigravity ACP provider.
 *
 * Antigravity ACP has no API key and no OAuth redirect of its own: credentials
 * live in the local `agy` CLI, one profile per Google account. This card drives
 * that login flow through /api/agy-acp so an account can be added from the
 * dashboard without touching a terminal.
 */
const STATUS = {
  ok: { variant: "success", label: "Ready" },
  tos: { variant: "error", label: "ToS blocked" },
  verify: { variant: "warning", label: "Needs verification" },
  error: { variant: "error", label: "Error" },
  untested: { variant: "default", label: "Untested" },
};

export default function AcpAccountsCard() {
  const notifySuccess = useNotificationStore((s) => s.success);
  const notifyError = useNotificationStore((s) => s.error);
  const [profiles, setProfiles] = useState([]);
  const [primary, setPrimary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [bridgeDown, setBridgeDown] = useState(false);
  const [busy, setBusy] = useState("");

  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [loginId, setLoginId] = useState(null);
  const [url, setUrl] = useState("");
  const [code, setCode] = useState("");
  const [result, setResult] = useState(null);
  const [starting, setStarting] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/agy-acp/profiles", { cache: "no-store" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `bridge error ${res.status}`);
      setProfiles(data.profiles || []);
      setPrimary(data.primary || null);
      setBridgeDown(false);
    } catch (e) {
      setBridgeDown(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  const startLogin = async () => {
    const profile = name.trim();
    if (!profile) return;
    setStarting(true);
    setResult(null);
    try {
      const res = await fetch("/api/agy-acp/login/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || data.detail || "start failed");
      setLoginId(data.loginId);
      setUrl(data.url || "");
    } catch (e) {
      setResult({ ok: false, text: e.message });
    } finally {
      setStarting(false);
    }
  };

  const submitCode = async () => {
    if (!loginId || !code.trim()) return;
    setSubmitting(true);
    try {
      const res = await fetch("/api/agy-acp/login/complete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ loginId, code: code.trim() }),
      });
      const data = await res.json();
      setResult({ ok: !!data.ok, text: data.detail || (data.ok ? "Logged in" : "Failed") });
      if (data.ok) {
        notifySuccess(data.detail || "Account added");
        load();
      }
    } catch (e) {
      setResult({ ok: false, text: e.message });
    } finally {
      setSubmitting(false);
    }
  };

  const testProfile = async (profile) => {
    setBusy(profile);
    try {
      const res = await fetch("/api/agy-acp/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile, model: "gemini-3.8-flash-medium" }),
      });
      const data = await res.json();
      const msg = `${profile}: ${data.status || (data.ok ? "ok" : "failed")} — ${(data.detail || "").slice(0, 90)}`;
      if (data.ok) notifySuccess(msg); else notifyError(msg);
      load();
    } catch (e) {
      notifyError(e.message);
    } finally {
      setBusy("");
    }
  };

  const makePrimary = async (profile) => {
    setBusy(profile);
    try {
      const res = await fetch("/api/agy-acp/primary", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile }),
      });
      const data = await res.json();
      if (data.ok) notifySuccess(`Primary: ${profile}`);
      load();
    } finally {
      setBusy("");
    }
  };

  const resetModal = () => {
    setOpen(false);
    setName("");
    setLoginId(null);
    setUrl("");
    setCode("");
    setResult(null);
  };

  return (
    <Card>
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold">Accounts</h2>
          <p className="text-xs text-text-muted mt-1">
            Google accounts signed in through the official <code>agy</code> CLI. One primary
            carries traffic; the rest are fallback.
          </p>
        </div>
        <Button size="sm" icon="person_add" onClick={() => setOpen(true)}>
          Add Account
        </Button>
      </div>

      {bridgeDown && (
        <div className="mb-3 rounded-lg border border-red-500/30 bg-red-500/5 px-3 py-2 text-xs text-red-500">
          Bridge unreachable on 127.0.0.1:20130 — run <code>systemctl restart agy-bridge</code>.
        </div>
      )}

      {loading ? (
        <p className="text-sm text-text-muted">Loading accounts…</p>
      ) : profiles.length === 0 ? (
        <p className="text-sm text-text-muted">
          No accounts yet. Add one to start routing through Antigravity.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          {profiles.map((p) => {
            const st = STATUS[p.status] || STATUS.untested;
            return (
              <div
                key={p.name}
                className="flex flex-col gap-2 rounded-lg border border-black/10 dark:border-white/10 px-3 py-2.5 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    {p.primary && (
                      <span className="material-symbols-outlined text-[15px] text-amber-500">
                        star
                      </span>
                    )}
                    <span className="truncate text-sm font-medium">{p.name}</span>
                    <Badge variant={st.variant} size="sm" dot>
                      {st.label}
                    </Badge>
                  </div>
                  {p.detail && (
                    <p className="mt-0.5 truncate text-[11px] text-text-muted">{p.detail}</p>
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <Button
                    size="xs"
                    variant="secondary"
                    icon="science"
                    loading={busy === p.name}
                    onClick={() => testProfile(p.name)}
                  >
                    Test
                  </Button>
                  {!p.primary && p.status === "ok" && (
                    <Button
                      size="xs"
                      variant="secondary"
                      icon="star"
                      onClick={() => makePrimary(p.name)}
                    >
                      Primary
                    </Button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      <Modal
        isOpen={open}
        onClose={resetModal}
        title="Add Antigravity account"
        size="lg"
        footer={
          loginId ? (
            <Button onClick={submitCode} loading={submitting} disabled={!code.trim()}>
              Sign in
            </Button>
          ) : (
            <Button onClick={startLogin} loading={starting} disabled={!name.trim()}>
              Get login link
            </Button>
          )
        }
      >
        <div className="flex flex-col gap-3">
          {!loginId ? (
            <>
              <p className="text-xs text-text-muted">
                Pick a name for this profile — the account email works well. Credentials are
                stored by the CLI under its own profile directory, so accounts never overwrite
                each other.
              </p>
              <Input
                label="Profile name"
                placeholder="permainan10st"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </>
          ) : (
            <>
              <p className="text-xs text-text-muted">
                1. Open the link and sign in with the Google account you want to add.
                <br />
                2. You land on <code>antigravity.google/oauth-callback?…&amp;code=…</code> — the
                page may look blank, that is normal.
                <br />
                3. Copy the <code>code=</code> value and paste it below. Codes expire in about
                two minutes.
              </p>
              {url ? (
                <a
                  href={url}
                  target="_blank"
                  rel="noreferrer"
                  className="break-all rounded-lg border border-black/10 dark:border-white/10 bg-black/5 dark:bg-white/5 px-3 py-2 text-[11px] text-primary hover:underline"
                >
                  {url}
                </a>
              ) : (
                <p className="text-xs text-text-muted">Waiting for the login URL…</p>
              )}
              <Input
                label="Authorization code"
                placeholder="4/0A..."
                value={code}
                onChange={(e) => setCode(e.target.value)}
              />
            </>
          )}
          {result && (
            <p className={`text-xs ${result.ok ? "text-green-500" : "text-red-500"}`}>
              {result.text}
            </p>
          )}
        </div>
      </Modal>
    </Card>
  );
}
