// Thin client for the StormLines orchestration server (backend/server.py).
// The frontend no longer bundles a single fixed storm — every layer is
// fetched at runtime, scoped to whichever area+date range the user picked.

const API_BASE = 'http://localhost:8000';

export async function createRequest(bbox, start, end) {
  const resp = await fetch(`${API_BASE}/api/requests`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bbox, start, end }),
  });
  if (!resp.ok) throw new Error(`createRequest failed: HTTP ${resp.status}`);
  return resp.json();
}

export async function getRequest(requestId) {
  const resp = await fetch(`${API_BASE}/api/requests/${requestId}`);
  if (!resp.ok) throw new Error(`getRequest failed: HTTP ${resp.status}`);
  return resp.json();
}

// Polls until the request reaches a terminal state (ready|failed).
export async function pollRequest(requestId, { intervalMs = 1500, onStatus } = {}) {
  for (;;) {
    const data = await getRequest(requestId);
    onStatus?.(data);
    if (data.status === 'ready' || data.status === 'failed') return data;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

// Outages/wind/precip — scoped per (bbox, start, end) request.
export function requestDataUrl(requestId, path) {
  return `${API_BASE}/data/requests/${requestId}/${path}`;
}

// Transmission line geometry — scoped per area only (doesn't vary by date).
export function infraDataUrl(areaHash, path) {
  return `${API_BASE}/data/infrastructure/${areaHash}/${path}`;
}
