import { useRef, useState } from 'react';
import { getIntelligence, generateIntelligence } from '../api/intelligence.js';
import { toApiError } from '../api/client.js';
import { relativeTime } from '../utils/format.js';

const LOCATION_TYPE_LABELS = {
  residential:  { label: 'Residential',  cls: 'intel-type--residential' },
  workplace:    { label: 'Workplace',     cls: 'intel-type--workplace'   },
  transit_hub:  { label: 'Transit Hub',  cls: 'intel-type--transit'     },
  mixed:        { label: 'Mixed Use',    cls: 'intel-type--mixed'       },
  unknown:      { label: 'Unknown',      cls: 'intel-type--unknown'     },
};

const CONFIDENCE_LABELS = {
  high:   { label: 'High confidence',   cls: 'intel-conf--high'   },
  medium: { label: 'Medium confidence', cls: 'intel-conf--medium' },
  low:    { label: 'Low confidence',    cls: 'intel-conf--low'    },
};

export default function IntelligencePanel({ deviceId }) {
  const [data, setData] = useState(null);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState(null);
  const [fetched, setFetched] = useState(false);
  const prevDeviceId = useRef(null);

  // Reset when device changes, but don't auto-fetch — user must click Generate.
  if (deviceId !== prevDeviceId.current) {
    prevDeviceId.current = deviceId;
    if (data !== null || error !== null || fetched) {
      setData(null);
      setError(null);
      setFetched(false);
    }
  }

  async function handleLoad() {
    if (!deviceId) return;
    setGenerating(false);
    setError(null);
    try {
      const result = await getIntelligence(deviceId);
      setData(result);
      setFetched(true);
    } catch (err) {
      const apiErr = toApiError(err);
      if (apiErr.code === 'not_found') {
        setData(null);
        setFetched(true);
      } else {
        setError(apiErr);
      }
    }
  }

  async function handleGenerate() {
    if (!deviceId) return;
    setGenerating(true);
    setError(null);
    try {
      const result = await generateIntelligence(deviceId);
      setData(result);
      setFetched(true);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setGenerating(false);
    }
  }

  if (!deviceId) return null;

  const typeInfo = LOCATION_TYPE_LABELS[data?.location_type] ?? LOCATION_TYPE_LABELS.unknown;
  const confInfo = CONFIDENCE_LABELS[data?.confidence] ?? null;

  return (
    <div className="panel intel-panel">
      <h2 className="panel-title">AI Intelligence</h2>

      {!fetched && !generating && (
        <div className="intel-empty">
          <p className="panel-empty">
            AI analysis reads 14 days of movement data and describes this device's
            daily routine, home area, and patterns in plain English.
          </p>
          <div className="intel-actions">
            <button className="btn btn--ghost btn--sm" onClick={handleLoad}>
              Check for saved analysis
            </button>
            <button className="btn btn--primary" onClick={handleGenerate}>
              Generate analysis
            </button>
          </div>
        </div>
      )}

      {generating && (
        <p className="panel-empty intel-generating">
          <span className="intel-spinner" /> Analysing 14 days of movement data…
        </p>
      )}

      {error && !generating && (
        <div className="intel-error">
          <p className="panel-empty panel-empty--error">{error.message}</p>
          <button className="btn btn--ghost btn--sm" onClick={handleGenerate}>
            Retry
          </button>
        </div>
      )}

      {fetched && !data && !generating && !error && (
        <div className="intel-empty">
          <p className="panel-empty">No saved analysis — generate one to get started.</p>
          <button className="btn btn--primary" onClick={handleGenerate}>
            Generate analysis
          </button>
        </div>
      )}

      {data && !generating && (
        <div className="intel-result">
          <div className="intel-header">
            <div className="intel-badges">
              <span className={`intel-type ${typeInfo.cls}`}>{typeInfo.label}</span>
              {confInfo && (
                <span className={`intel-conf ${confInfo.cls}`}>{confInfo.label}</span>
              )}
            </div>
            {data.home_area && (
              <span className="intel-home-area">{data.home_area}</span>
            )}
          </div>

          {data.summary && (
            <p className="intel-summary">{data.summary}</p>
          )}

          {data.patterns?.length > 0 && (
            <div className="intel-section">
              <h3 className="intel-section-title">Patterns</h3>
              <ul className="intel-list">
                {data.patterns.map((p, i) => (
                  <li key={i} className="intel-list__item intel-list__item--pattern">{p}</li>
                ))}
              </ul>
            </div>
          )}

          {data.anomalies?.length > 0 && (
            <div className="intel-section">
              <h3 className="intel-section-title">Anomalies</h3>
              <ul className="intel-list">
                {data.anomalies.map((a, i) => (
                  <li key={i} className="intel-list__item intel-list__item--anomaly">{a}</li>
                ))}
              </ul>
            </div>
          )}

          <div className="intel-footer">
            <span className="intel-meta">
              Generated {relativeTime(data.generated_at)} · {data.model_used}
            </span>
            <button className="btn btn--ghost btn--sm" onClick={handleGenerate}>
              Regenerate
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
