/**
 * Compare a mounted screen with its flow screen from the pinned Flow Lab
 * contract (flow.contract.json). Only what a person can name is compared:
 * headings, pills, buttons, links, form labels and rows. Plain text, media
 * and the notes are not checked.
 */
import contractFile from './flow.contract.json';
import gapsFile from './flow-gaps.json';

export interface FlowElement {
  k: string;
  text: string;
  meta?: string;
  to?: string | null;
}
export interface ScreenContract {
  id: string;
  hash: string;
  notesHash: string;
  elements: FlowElement[];
}
export interface FlowContract {
  format: string;
  flow: string;
  rev: number;
  screens: ScreenContract[];
}
/** Known differences, by the item labels that `checkScreen` prints. */
export interface FlowGaps {
  /** Not built yet: a whole screen (no `items`) or some items of it. */
  leftOut: { screen: string; release: string; items?: string[] }[];
  /** Extra items that a flow note describes, so they are not drift. */
  allowed: { screen: string; state?: string; items: string[]; why: string }[];
  /** Drift that is known and has a ticket or a decision. It stays visible as drift. */
  drift: { screen: string; state?: string; items: string[]; why: string }[];
}
export interface ScreenResult {
  screen: string;
  /** A second state of the screen, checked against the same flow screen. */
  state?: string;
  hash: string;
  status: 'ok' | 'drift';
  leftOut: { item: string; release: string }[];
  missing: string[];
  extra: string[];
}

export const contract = contractFile as FlowContract;
export const gaps = gapsFile as FlowGaps;

type Kind = 'h1' | 'h2' | 'pill' | 'notice' | 'button' | 'link' | 'field' | 'check' | 'row';
interface Candidate {
  kind: Kind;
  text: string;
}

const norm = (s: string | null | undefined) => (s ?? '').replace(/\s+/g, ' ').trim();
/** "a | b" and "a b" are the same row: markup decides where the bar goes. */
const squash = (s: string) => norm(s.replace(/\|/g, ' '));
const unback = (s: string) => norm(s.replace(/^</, ''));

function visible(el: Element): boolean {
  for (let e: Element | null = el; e; e = e.parentElement) {
    if (e instanceof HTMLElement && (e.hidden || e.style.display === 'none')) return false;
  }
  return true;
}

/** Text without form controls, so a label does not carry its select options. */
function ownText(el: Element): string {
  const copy = el.cloneNode(true) as Element;
  copy.querySelectorAll('input, select, textarea').forEach((c) => c.remove());
  return norm(copy.textContent);
}

/** "So 25.10. · Brunchtime | Upcoming · Live": a row's parts joined like the flow writes them. */
function rowText(el: Element): string {
  const parts = [...el.children].map((c) => norm(c.textContent)).filter(Boolean);
  if (parts.length > 1) return parts.join(' | ');
  if (el.children.length === 1) return rowText(el.children[0]);
  return norm(el.textContent);
}

function candidates(root: Element): Candidate[] {
  const out: Candidate[] = [];
  const add = (kind: Kind, selector: string, text: (el: Element) => string) =>
    root.querySelectorAll(selector).forEach((el) => {
      const t = visible(el) ? text(el) : '';
      if (t) out.push({ kind, text: t });
    });
  add('h1', 'h1', (el) => norm(el.textContent));
  add('h2', 'h2', (el) => norm(el.textContent));
  add('pill', '.pill', (el) => norm(el.textContent));
  add('notice', '[role="alert"], .notice', (el) => norm(el.textContent));
  add('button', 'button', (el) => norm(el.getAttribute('aria-label')) || norm(el.textContent));
  add('link', 'a', rowText);
  root.querySelectorAll('label').forEach((el) => {
    if (!visible(el)) return;
    const box = el.querySelector('input[type="checkbox"], input[type="radio"]');
    const field = norm(el.querySelector('.label')?.textContent) || ownText(el);
    // A trailing * only marks a required field.
    const text = box ? ownText(el) : field.replace(/\s*\*$/, '');
    if (text) out.push({ kind: box ? 'check' : 'field', text });
  });
  // A field with two inputs (start and end time) has a .label but no <label>.
  add('field', '.label', (el) => (el.closest('label') ? '' : norm(el.textContent)));
  add('row', 'li, dl > div', (el) => (el.querySelector('a') ? '' : rowText(el)));
  return out;
}

/** Items a page may show as plain text: a notice, a status line, a name in the nav. */
const plain = (e: FlowElement) => e.k === 'notice' || e.k === 'pill' || (e.k === 'nav' && !e.to);

const joined = (e: FlowElement) => (e.meta ? `${e.text} | ${e.meta}` : e.text);

/** How `checkScreen` names a flow item in its output and in flow-gaps.json. */
function label(e: FlowElement): string {
  return `${e.k}: ${joined(e)}`;
}

/** Flow items a check looks for. Plain text, media and notes are not checked. */
const CHECKED = new Set([
  'h1',
  'h2',
  'pill',
  'back',
  'nav',
  'button',
  'choice',
  'field',
  'check',
  'row',
  'notice',
]);

/** Does this candidate show this flow item? */
function shows(e: FlowElement, c: Candidate): boolean {
  switch (e.k) {
    case 'h1':
    case 'h2':
    case 'pill':
    case 'notice':
      return c.kind === e.k && c.text === e.text;
    case 'back':
      return (c.kind === 'button' || c.kind === 'link') && unback(c.text) === e.text;
    case 'nav':
    case 'button':
      return (c.kind === 'button' || c.kind === 'link') && c.text === e.text;
    case 'choice':
      return ['check', 'button', 'link'].includes(c.kind) && c.text === e.text;
    case 'field':
      return c.kind === 'field' && c.text === e.text;
    case 'check':
      return c.kind === 'check' && squash(c.text) === squash(joined(e));
    case 'row':
      return ['row', 'link', 'button'].includes(c.kind) && squash(c.text) === squash(joined(e));
    default:
      return false;
  }
}

export interface CheckOutcome {
  result: ScreenResult;
  /** Drift that flow-gaps.json does not name. Must be empty. */
  unexplained: string[];
  /** Entries in flow-gaps.json that no longer apply. Must be empty. */
  stale: string[];
}

/** Check a mounted screen (or one more `state` of it) against the pinned flow and flow-gaps.json. */
export function checkScreen(id: string, root: Element, state?: string): CheckOutcome {
  const screen = contract.screens.find((s) => s.id === id);
  if (!screen) throw new Error(`Flow rev ${contract.rev} has no screen "${id}"`);
  return compareScreen(screen, gaps, root, state);
}

export function compareScreen(
  screen: ScreenContract,
  gaps: FlowGaps,
  root: Element,
  state?: string
): CheckOutcome {
  const id = screen.id;
  // Left-out items are missing in every state; allowed and known drift belong to one state.
  const of = <T extends { screen: string; state?: string }>(list: T[], anyState = false) =>
    list.filter((g) => g.screen === id && (anyState || g.state === state));
  if (of(gaps.leftOut, true).some((g) => !g.items)) {
    throw new Error(`"${id}" is left out as a whole screen in flow-gaps.json, so it has no check`);
  }
  const leftOut = new Map(
    of(gaps.leftOut, true).flatMap((g) => g.items!.map((i) => [i, g.release]))
  );
  const allowed = new Set(of(gaps.allowed).flatMap((g) => g.items));
  const known = new Set(of(gaps.drift).flatMap((g) => g.items));

  const found = candidates(root);
  const text = norm(root.textContent);
  const result: ScreenResult = {
    screen: id,
    ...(state ? { state } : {}),
    hash: screen.hash,
    status: 'ok',
    leftOut: [],
    missing: [],
    extra: [],
  };
  const stale: string[] = [];
  for (const e of screen.elements) {
    if (!CHECKED.has(e.k)) continue;
    const there = found.some((c) => shows(e, c)) || (plain(e) && text.includes(e.text));
    const release = leftOut.get(label(e));
    if (there && release) stale.push(`left out but shown: ${label(e)}`);
    else if (!there && release) result.leftOut.push({ item: label(e), release });
    else if (!there) result.missing.push(label(e));
  }
  for (const c of found) {
    if (c.kind === 'row' || screen.elements.some((e) => shows(e, c))) continue;
    const item = `${c.kind}: ${c.text}`;
    if (!allowed.has(item) && !result.extra.includes(item)) result.extra.push(item);
  }
  for (const item of allowed) {
    if (!found.some((c) => `${c.kind}: ${c.text}` === item))
      stale.push(`allowed but not shown: ${item}`);
  }
  const drift = [...result.missing, ...result.extra];
  for (const item of leftOut.keys()) {
    if (!screen.elements.some((e) => label(e) === item))
      stale.push(`left out but not in the flow: ${item}`);
  }
  for (const item of known) if (!drift.includes(item)) stale.push(`known drift is gone: ${item}`);
  if (drift.length) result.status = 'drift';
  return { result, unexplained: drift.filter((d) => !known.has(d)), stale };
}
