import { useMemo } from 'react';
import { analyzeIntegrity, integrityScore } from '../utils/integrity.js';

const ICON = { pass: '✓', warn: '!', fail: '✗', unknown: '–' };
const LABEL = { pass: 'Pass', warn: 'Warning', fail: 'Fail', unknown: 'Unknown' };

/**
 * Device integrity panel — computed from position history plus fixture/API
 * static signals (timezone, IP, GPS spoofing apps, WiFi, app state).
 * Shows a check-by-check breakdown inspired by AddressIQ's verification view.
 */
export default function IntegrityPanel({ deviceId, positions = [], loading = false }) {
  const checks = useMemo(
    () => (positions.length > 0 ? analyzeIntegrity(deviceId, positions) : []),
    [deviceId, positions],
  );

  const score = useMemo(() => integrityScore(checks), [checks]);

  if (!deviceId) return null;

  const scoreState = score.failed > 0 ? 'fail' : score.warned > 0 ? 'warn' : 'ok';

  return (
    <section className="panel panel--integrity" aria-label="Device integrity checks">
      <header className="panel__head">
        <h2>
          <span className="integrity-shield" aria-hidden="true">⛨</span>
          Integrity
          {checks.length > 0 ? (
            <span className={`pill pill--${scoreState}`}>
              {score.passed}/{score.total}
            </span>
          ) : null}
        </h2>
        {score.failed > 0 ? (
          <span className="integrity-summary integrity-summary--fail">
            {score.failed} fail{score.failed > 1 ? 's' : ''}
          </span>
        ) : score.warned > 0 ? (
          <span className="integrity-summary integrity-summary--warn">
            {score.warned} warning{score.warned > 1 ? 's' : ''}
          </span>
        ) : checks.length > 0 ? (
          <span className="integrity-summary integrity-summary--ok">All clear</span>
        ) : null}
      </header>

      {loading && positions.length === 0 ? (
        <p className="notice">Analyzing device signals…</p>
      ) : positions.length === 0 ? (
        <p className="notice">
          No position history yet.
          <span className="notice__sub">
            History loads automatically when a device is selected.
          </span>
        </p>
      ) : (
        <ul className="integrity-list">
          {checks.map((check) => (
            <li key={check.id} className={`integrity-item integrity-item--${check.status}`}>
              <span
                className={`integrity-icon integrity-icon--${check.status}`}
                aria-label={LABEL[check.status]}
              >
                {ICON[check.status]}
              </span>
              <span className="integrity-body">
                <span className="integrity-name">{check.name}</span>
                <span className="integrity-detail">{check.detail}</span>
              </span>
              <span className={`integrity-badge integrity-badge--${check.status}`}>
                {LABEL[check.status]}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
