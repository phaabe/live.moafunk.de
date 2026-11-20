import { streamRecordingMimeType } from './useAudioCapture';

/** The oldest version of each browser a host can stream with. */
export const BROWSER_FLOOR: Record<string, number> = {
  Chrome: 100,
  Edge: 100,
  Firefox: 100,
  // Safari records WebM only from 18.4.
  Safari: 18.4,
};

export interface Browser {
  name: string;
  /** Major version; with the minor for Safari ("18.4"). */
  version: number;
}

/** The browser behind a user agent string; null when it is none of the known ones. */
export function detectBrowser(userAgent: string): Browser | null {
  const version = (pattern: RegExp) => {
    const match = userAgent.match(pattern);
    return match ? Number(match[1].replace('_', '.')) : null;
  };
  // Every iOS browser is Safari's engine, at the version of iOS.
  const ios = /iPhone|iPad|iPod/.test(userAgent) ? version(/OS (\d+(?:_\d+)?)/) : null;
  if (ios !== null) return { name: 'Safari', version: ios };
  const found: [string, number | null][] = [
    ['Edge', version(/Edg\/(\d+)/)],
    ['Firefox', version(/Firefox\/(\d+)/)],
    // Opera, Brave and the like are Chrome at this version.
    ['Chrome', version(/Chrome\/(\d+)/)],
    ['Safari', /Safari\//.test(userAgent) ? version(/Version\/(\d+(?:\.\d+)?)/) : null],
  ];
  for (const [name, v] of found) if (v !== null) return { name, version: v };
  return null;
}

/** What streaming needs from the browser. */
function canStream(): boolean {
  return (
    streamRecordingMimeType() !== null &&
    typeof navigator.mediaDevices?.getUserMedia === 'function' &&
    typeof AudioContext !== 'undefined' &&
    typeof WebSocket !== 'undefined'
  );
}

const ADVICE =
  'Update it before your show, or use a current Chrome, Edge or Firefox. Safari works from 18.4.';

/**
 * Why a host cannot stream with this browser, or null when they can: it is
 * older than the floor, or it lacks something streaming needs.
 */
export function browserProblem(
  userAgent: string = navigator.userAgent,
  able: boolean = canStream()
): string | null {
  const browser = detectBrowser(userAgent);
  const tooOld = browser !== null && browser.version < BROWSER_FLOOR[browser.name];
  if (able && !tooOld) return null;
  if (!browser) return `This browser cannot stream. ${ADVICE}`;
  const what = tooOld ? 'is too old for streaming' : 'cannot stream';
  return `Your browser (${browser.name} ${browser.version}) ${what}. ${ADVICE}`;
}
