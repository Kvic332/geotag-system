// Shared axios instance + error normalisation for every backend call.
//
// API-CONTRACT.md: base URL from VITE_API_URL, every route needs
// `Authorization: Bearer <jwt>`, and errors are always
//   { "error": { "code": "<machine_code>", "message": "<human text>" } }
// with codes unauthorized | forbidden | not_found | validation_error | internal_error.

import axios from 'axios';
import { getToken } from './token.js';

export const API_URL = (import.meta.env.VITE_API_URL || 'http://localhost:3000').replace(/\/+$/, '');

export const USE_FIXTURES = String(import.meta.env.VITE_USE_FIXTURES || '').toLowerCase() === 'true';

export const http = axios.create({
  baseURL: API_URL,
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
});

http.interceptors.request.use((config) => {
  const token = getToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

/**
 * Turn anything axios can throw into a flat, renderable shape:
 * { code, message, status } where `code` is a contract code, or one of the
 * client-side codes below when the request never reached the API.
 *
 *   no_token      — nothing pasted into the dev-token box yet
 *   network_error — connection refused / DNS / CORS-blocked (backend down)
 *   timeout       — request exceeded the client timeout
 */
export function toApiError(err) {
  if (err && err.__apiError) return err;

  const error = new Error('API request failed');
  error.__apiError = true;
  error.status = null;
  error.code = 'internal_error';
  error.message = 'Unexpected error.';

  if (axios.isAxiosError(err)) {
    if (err.response) {
      error.status = err.response.status;
      const body = err.response.data;
      const contractError = body && typeof body === 'object' ? body.error : null;
      if (contractError && typeof contractError === 'object') {
        error.code = contractError.code || fallbackCodeForStatus(err.response.status);
        error.message = contractError.message || `Request failed with status ${err.response.status}.`;
      } else {
        error.code = fallbackCodeForStatus(err.response.status);
        error.message =
          typeof body === 'string' && body.trim()
            ? body.trim().slice(0, 300)
            : `Request failed with status ${err.response.status}.`;
      }
    } else if (err.code === 'ECONNABORTED') {
      error.code = 'timeout';
      error.message = `No response from ${API_URL} within 30s.`;
    } else {
      error.code = 'network_error';
      error.message = `Cannot reach the API at ${API_URL}. Is the backend running?`;
    }
  } else if (err instanceof Error) {
    error.message = err.message;
  }

  return error;
}

function fallbackCodeForStatus(status) {
  if (status === 400) return 'validation_error';
  if (status === 401) return 'unauthorized';
  if (status === 403) return 'forbidden';
  if (status === 404) return 'not_found';
  return 'internal_error';
}

export function makeError(code, message, status = null) {
  const error = new Error(message);
  error.__apiError = true;
  error.code = code;
  error.status = status;
  return error;
}

/** Wrap a request so callers only ever see normalised errors. */
export async function request(config) {
  try {
    const response = await http.request(config);
    return response.data;
  } catch (err) {
    throw toApiError(err);
  }
}
