/**
 * Detects if the current platform is iOS
 */
export function isIOSDevice(): boolean {
  const platform = (navigator as Navigator & { userAgentData?: { platform: string } }).userAgentData?.platform || navigator?.platform || 'unknown';
  return /iPhone|iPod|iPad/.test(platform);
}

/**
 * Checks if the stream is currently live by making a HEAD request
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
