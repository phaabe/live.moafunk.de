/**
 * Detects if the current platform is iOS
 */
export function isIOSDevice(): boolean {
  const platform =
    (navigator as Navigator & { userAgentData?: { platform: string } }).userAgentData?.platform ||
    navigator?.platform ||
    'unknown';
  return /iPhone|iPod|iPad/.test(platform);
}

/**
 * Checks if the stream is currently live by making a HEAD request.
 * Used for the legacy NMS path (HLS mount exists only while broadcasting).
 */
export async function checkStreamStatus(url: string): Promise<boolean> {
  try {
    const response = await fetch(url, { method: 'HEAD' });
    return response.status === 200;
  } catch (error) {
    console.error('Stream status check error:', error);
    return false;
  }
}

/**
 * Checks live status via the backend's authoritative on-air endpoint
 * (`GET /api/stream/status` → `{ active: boolean }`).
 *
 * Required in Icecast mode: the public mount is mksafe-backed and therefore
 * always up (serves silence between shows), so — unlike the NMS HLS mount —
 * its existence can't distinguish "live" from "dead air". The backend knows
 * the real state because the broadcaster streams in over its WebSocket.
 */
export async function checkBackendLive(url: string): Promise<boolean> {
  try {
    const response = await fetch(url, { method: 'GET' });
    if (!response.ok) return false;
    const data = (await response.json()) as { active?: boolean };
    return data.active === true;
  } catch (error) {
    console.error('Backend stream status check error:', error);
    return false;
  }
}
