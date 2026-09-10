import { useState } from 'react';
import { describeToken, getToken, setToken } from '../api/token.js';

/**
 * Dev-token box (SPEC-DECISIONS D6).
 *
 * The operator pastes a JWT minted by `backend/scripts/mint_dev_token.py`; it is
 * kept in localStorage and sent as `Authorization: Bearer <jwt>` on every request.
 * No token is ever hardcoded or committed. The tenant shown is decoded from the
 * `custom:tenant_id` claim purely for display — the backend does the verifying.
 */
export default function TokenBar({ onTokenChange }) {
  const [value, setValue] = useState(() => getToken());
  const [open, setOpen] = useState(() => !getToken());
  const info = describeToken();

  const apply = (next) => {
    setToken(next);
    setValue(next);
    if (onTokenChange) onTokenChange();
  };

  const summary = () => {
    if (!info) return 'no token';
    if (info.malformed) return 'token set (not a readable JWT)';
    if (info.expired) return `expired · tenant ${info.tenantId ?? '—'}`;
    return `tenant ${info.tenantId ?? '(no custom:tenant_id claim)'}`;
  };

  const state = !info ? 'none' : info.malformed || info.expired || !info.tenantId ? 'warn' : 'ok';

  return (
    <div className="tokenbar">
      <button
        type="button"
        className={`token-chip token-chip--${state}`}
        onClick={() => setOpen((current) => !current)}
        aria-expanded={open}
      >
        <i className="dot" />
        {summary()}
      </button>

      {open ? (
        <form
          className="token-form"
          onSubmit={(event) => {
            event.preventDefault();
            apply(value);
            if (value.trim()) setOpen(false);
          }}
        >
          <label className="field">
            <span className="sr-only">Dev JWT</span>
            <input
              type="password"
              value={value}
              placeholder="Paste the dev JWT (mint_dev_token.py)"
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => setValue(event.target.value)}
            />
          </label>
          <button type="submit" className="btn btn--primary btn--sm">
            Use token
          </button>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => apply('')}
          >
            Clear
          </button>
          <p className="hint hint--tight">
            Stored in this browser only (localStorage). Mint one with{' '}
            <code>python backend/scripts/mint_dev_token.py</code>.
          </p>
        </form>
      ) : null}
    </div>
  );
}
