// Before the local run: only the fake mic's tone. No staging.
import { tone } from './global-setup';
import { TONE_WAV } from './lib/staging';

export default function localSetup(): void {
  tone(440, 10, TONE_WAV, []);
}
