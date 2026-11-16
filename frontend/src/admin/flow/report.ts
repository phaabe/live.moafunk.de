/**
 * The flow-sync.json that a build serves at /flow-sync.json: per flow
 * screen, how this build stands against the pinned flow. Flow Lab's
 * `npm run sync-status` reads it from each environment.
 */
import type { FlowContract, FlowGaps, ScreenResult } from './check';

export interface FlowSyncScreen {
  hash: string;
  notesHash: string;
  status: 'ok' | 'drift' | 'left-out';
  release?: string;
  leftOut: { item: string; release: string }[];
  /** Items of a second state carry it in front: "[reconnecting] row: …". */
  missing: string[];
  extra: string[];
}
export interface FlowSync {
  format: 'flow-sync';
  version: 1;
  flow: string;
  rev: number;
  build: string;
  screens: Record<string, FlowSyncScreen>;
}

export function flowSync(
  contract: FlowContract,
  gaps: FlowGaps,
  results: ScreenResult[],
  build: string
): FlowSync {
  const screens: Record<string, FlowSyncScreen> = {};
  for (const s of contract.screens) {
    const out: FlowSyncScreen = {
      hash: s.hash,
      notesHash: s.notesHash,
      status: 'ok',
      leftOut: [],
      missing: [],
      extra: [],
    };
    screens[s.id] = out;
    const whole = gaps.leftOut.find((g) => g.screen === s.id && !g.items);
    if (whole) {
      Object.assign(out, { status: 'left-out', release: whole.release });
      continue;
    }
    const mine = results.filter((r) => r.screen === s.id);
    const main = mine.find((r) => !r.state);
    // The tests fail on these too; the report must not hide them.
    if (!main) out.missing.push('screen: no check');
    for (const r of mine) {
      const tag = r.state ? `[${r.state}] ` : '';
      if (r.hash !== s.hash) out.missing.push(`${tag}screen: checked against an older flow`);
      else {
        if (!r.state) out.leftOut = r.leftOut;
        out.missing.push(...r.missing.map((m) => tag + m));
        out.extra.push(...r.extra.map((x) => tag + x));
      }
    }
    if (out.missing.length || out.extra.length) out.status = 'drift';
  }
  return {
    format: 'flow-sync',
    version: 1,
    flow: contract.flow,
    rev: contract.rev,
    build,
    screens,
  };
}
