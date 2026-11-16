// Before the run: make the fake mic's tone and the uploaded set, and hold
// staging so no deploy lands mid-show (a hold someone else set stays theirs).
import { execFileSync } from 'node:child_process';
import { RUN, SET_MP3, TONE_WAV, staging } from './lib/staging';

function tone(frequency: number, seconds: number, out: string, codec: string[]): void {
  execFileSync('ffmpeg', [
    ...['-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi'],
    ...['-i', `sine=frequency=${frequency}:sample_rate=48000:duration=${seconds}`],
    ...['-ac', '2', ...codec, out],
  ]);
}

export default function globalSetup(): void {
  // Chromium loops the file as its fake microphone.
  tone(440, 10, TONE_WAV, []);
  tone(440, 70, SET_MP3, ['-c:a', 'libmp3lame', '-b:a', '128k']);
  console.log(staging(`e2e-hold ${RUN}`));
}
