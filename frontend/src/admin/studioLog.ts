import { showsApi, type StudioLog, type StudioSoundcheckLog } from '@admin/api';
import type { useAudioCapture, SoundcheckStep } from '@admin/composables';

/**
 * The host's browser saves what the Studio and the live panel measured to the
 * episode (`PUT /api/shows/:id/studio-log`): the soundcheck at Go Live, the
 * live panel every 15 s while on air and at End Episode. The episode page
 * shows it read-only to other hosts while on air and to everyone after the
 * show. Best effort: the broadcast never waits for it, and a failed save is
 * replaced by the next one.
 */

/** How often the live panel saves its values while on air. */
export const STUDIO_LOG_SAVE_MS = 15_000;

/** The name of the captured input, as the Studio shows it. */
export function inputName(capture: ReturnType<typeof useAudioCapture>): string {
  const id = capture.selectedDeviceId.value;
  if (id === 'screen') return 'Screen audio';
  return capture.devices.value.find((d) => d.deviceId === id)?.label || 'Audio input';
}

export function saveStudioLog(showId: number, part: StudioLog): Promise<void> {
  return showsApi.saveStudioLog(showId, part).catch(() => undefined);
}

/** At Go Live: the input and each check this show went live with. */
export function saveSoundcheck(
  showId: number,
  input: string,
  steps: readonly SoundcheckStep[]
): Promise<void> {
  const soundcheck: StudioSoundcheckLog = {
    at: Date.now(),
    input,
    checks: steps.map(({ id, name, state, detail }) => ({ id, name, state, detail })),
  };
  return saveStudioLog(showId, { soundcheck });
}
