// Dev/auth token storage.
//
// SPEC-DECISIONS D6: every request carries `Authorization: Bearer <jwt>` and the
// tenant is read from the token's `custom:tenant_id` claim. Locally the token is
// minted by `backend/scripts/mint_dev_token.py`.
//
// The token is NEVER hardcoded and never committed. It lives in localStorage,
// pasted by the operator. VITE_DEV_JWT may pre-seed it for convenience, but the
// committed .env.local.example leaves it empty.

const STORAGE_KEY = 'geotag.jwt';

const seeded = String(import.meta.env.VITE_DEV_JWT || '').trim();

let cached = null;

function readStorage() {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    // Private mode / storage blocked — fall back to in-memory only.
    return null;
  }
}

export function getToken() {
  if (cached !== null) return cached;
  const stored = readStorage();
  cached = (stored ?? seeded ?? '').trim();
  return cached;
}

export function setToken(value) {
  cached = String(value || '').trim();
  try {
    if (cached) window.localStorage.setItem(STORAGE_KEY, cached);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Ignore: the in-memory copy still works for this tab.
  }
  return cached;
}

export function hasToken() {
  return getToken().length > 0;
}

/**
 * Best-effort, UNVERIFIED decode of the JWT payload, purely so the header can
 * show which tenant/subject the pasted token belongs to. Signature validation is
 * the backend's job (D6) — nothing here is a security check.
 */
export function describeToken() {
  const token = getToken();
  if (!token) return null;
  const parts = token.split('.');
  if (parts.length !== 3) return { tenantId: null, subject: null, malformed: true };
  try {
    const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const payload = JSON.parse(atob(base64.padEnd(Math.ceil(base64.length / 4) * 4, '=')));
    const exp = typeof payload.exp === 'number' ? new Date(payload.exp * 1000) : null;
    return {
      tenantId: payload['custom:tenant_id'] ?? null,
      subject: payload.sub ?? null,
      expiresAt: exp,
      expired: exp ? exp.getTime() < Date.now() : false,
      malformed: false,
    };
  } catch {
    return { tenantId: null, subject: null, malformed: true };
  }
}
