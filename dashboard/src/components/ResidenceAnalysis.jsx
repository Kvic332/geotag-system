import { useEffect, useRef, useState } from 'react';
import { getResidence } from '../api/residence.js';
import { toApiError } from '../api/client.js';
import { formatCoord } from '../utils/format.js';

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN;

// Haversine distance in metres between two lat/lng pairs.
function haversineMetres(lat1, lng1, lat2, lng2) {
  const R = 6_371_000;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLng = toRad(lng2 - lng1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLng / 2) ** 2;
  return R * 2 * Math.asin(Math.sqrt(a));
}

async function geocodeAddress(text) {
  if (!MAPBOX_TOKEN || !text.trim()) return null;
  const url = `https://api.mapbox.com/geocoding/v5/mapbox.places/${encodeURIComponent(text)}.json?access_token=${MAPBOX_TOKEN}&types=address&limit=1&country=ng`;
  const res = await fetch(url);
  const data = await res.json();
  const feature = data.features?.[0];
  if (!feature) return null;
  const [lng, lat] = feature.center;
  return { lat, lng, formatted: feature.place_name };
}

async function reverseGeocode(lat, lng) {
  if (!MAPBOX_TOKEN) return null;
  const url = `https://api.mapbox.com/geocoding/v5/mapbox.places/${lng},${lat}.json?access_token=${MAPBOX_TOKEN}&types=address&limit=1&country=ng`;
  const res = await fetch(url);
  const data = await res.json();
  return data.features?.[0]?.place_name ?? null;
}

function formatDistance(metres) {
  if (metres < 1000) return `${Math.round(metres)} m`;
  return `${(metres / 1000).toFixed(1)} km`;
}

export default function ResidenceAnalysis({ deviceId }) {
  const [state, setState] = useState({ loading: false, error: null, data: null });
  const [detectedAddress, setDetectedAddress] = useState(null);
  const [addressLoading, setAddressLoading] = useState(false);
  const [statedAddress, setStatedAddress] = useState('');
  const [verifying, setVerifying] = useState(false);
  const [verifyResult, setVerifyResult] = useState(null);
  const prevDeviceId = useRef(null);

  useEffect(() => {
    if (!deviceId) {
      setState({ loading: false, error: null, data: null });
      setDetectedAddress(null);
      setStatedAddress('');
      setVerifyResult(null);
      return;
    }

    if (deviceId === prevDeviceId.current) return;
    prevDeviceId.current = deviceId;

    setState({ loading: true, error: null, data: null });
    setDetectedAddress(null);
    setAddressLoading(false);
    setStatedAddress('');
    setVerifyResult(null);

    let cancelled = false;
    getResidence(deviceId)
      .then((data) => {
        if (cancelled) return;
        setState({ loading: false, error: null, data });
        if (MAPBOX_TOKEN) {
          setAddressLoading(true);
          reverseGeocode(data.cluster_lat, data.cluster_lng).then((addr) => {
            if (!cancelled) {
              setDetectedAddress(addr);
              setAddressLoading(false);
            }
          });
        }
      })
      .catch((err) => {
        if (cancelled) return;
        const apiErr = toApiError(err);
        if (apiErr.code === 'not_found') {
          setState({ loading: false, error: null, data: null });
        } else {
          setState({ loading: false, error: apiErr, data: null });
        }
      });

    return () => { cancelled = true; };
  }, [deviceId]);

  async function handleVerify(e) {
    e.preventDefault();
    if (!statedAddress.trim() || !state.data) return;
    setVerifying(true);
    setVerifyResult(null);
    try {
      const geo = await geocodeAddress(statedAddress);
      if (!geo) {
        setVerifyResult({ ok: false, message: 'Could not geocode stated address — try being more specific.' });
        return;
      }
      const dist = haversineMetres(state.data.cluster_lat, state.data.cluster_lng, geo.lat, geo.lng);
      const match = dist <= 300;
      setVerifyResult({
        ok: match,
        distance: dist,
        formatted: geo.formatted,
        message: match
          ? `Match — detected residence is ${formatDistance(dist)} from stated address.`
          : `Mismatch — detected residence is ${formatDistance(dist)} away from stated address.`,
      });
    } catch {
      setVerifyResult({ ok: false, message: 'Verification failed — geocoding error.' });
    } finally {
      setVerifying(false);
    }
  }

  if (!deviceId) return null;
  if (state.loading) return <div className="panel residence-panel"><p className="panel-empty">Analysing night-time patterns…</p></div>;
  if (state.error) return <div className="panel residence-panel"><p className="panel-empty panel-empty--error">Residence: {state.error.message}</p></div>;
  if (!state.data) return <div className="panel residence-panel"><p className="panel-empty">No night-time pings in the last 14 days.</p></div>;

  const { cluster_lat, cluster_lng, night_pings, total_night_pings, days_analyzed, confidence_pct } = state.data;
  const confidenceClass = confidence_pct >= 70 ? 'badge--high' : confidence_pct >= 40 ? 'badge--mid' : 'badge--low';

  return (
    <div className="panel residence-panel">
      <h2 className="panel-title">Residence Detection</h2>

      <div className="residence-summary">
        <div className="residence-location">
          {addressLoading && (
            <span className="residence-address residence-address--loading">Looking up address…</span>
          )}
          {!addressLoading && detectedAddress && (
            <span className="residence-address">{detectedAddress}</span>
          )}
          {!addressLoading && !detectedAddress && (
            <span className="residence-coords">{formatCoord(cluster_lat)}, {formatCoord(cluster_lng)}</span>
          )}
          {detectedAddress && (
            <a
              className="residence-coords residence-map-link"
              href={`https://maps.google.com/?q=${cluster_lat},${cluster_lng}`}
              target="_blank"
              rel="noopener noreferrer"
            >
              {formatCoord(cluster_lat)}, {formatCoord(cluster_lng)} ↗
            </a>
          )}
        </div>
        <div className="residence-stats">
          <span className={`badge ${confidenceClass}`}>{confidence_pct}% confidence</span>
          <span className="stat-item">{night_pings}/{total_night_pings} night pings</span>
          <span className="stat-item">{days_analyzed} night{days_analyzed !== 1 ? 's' : ''} of data</span>
        </div>
      </div>

      <form className="residence-verify" onSubmit={handleVerify}>
        <label className="field-label" htmlFor="stated-address">Verify stated address</label>
        <div className="residence-verify__row">
          <input
            id="stated-address"
            className="text-input"
            type="text"
            placeholder="e.g. 12 Amuwo Street, Festac, Lagos"
            value={statedAddress}
            onChange={(e) => { setStatedAddress(e.target.value); setVerifyResult(null); }}
          />
          <button className="btn btn--primary" type="submit" disabled={verifying || !statedAddress.trim()}>
            {verifying ? 'Checking…' : 'Verify'}
          </button>
        </div>
        {verifyResult && (
          <p className={`residence-result residence-result--${verifyResult.ok ? 'match' : 'mismatch'}`}>
            {verifyResult.ok ? '✓' : '✗'} {verifyResult.message}
            {verifyResult.formatted && <span className="residence-result__addr"> ({verifyResult.formatted})</span>}
          </p>
        )}
      </form>
    </div>
  );
}
