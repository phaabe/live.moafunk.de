import { describe, expect, it } from 'vitest';
import {
  compareScreen,
  contract,
  gaps,
  type FlowGaps,
  type ScreenContract,
  type ScreenResult,
} from '../flow/check';
import { flowSync } from '../flow/report';

// What each screen's flow case wrote (see expectFlow): the base of /flow-sync.json.
const files = import.meta.glob<ScreenResult>('../flow/sync/*.json', {
  eager: true,
  import: 'default',
});
const results = Object.entries(files).map(([path, result]) => ({
  name: path.replace(/^.*\/(.+)\.json$/, '$1'),
  result,
}));
const wholeLeftOut = (id: string) => gaps.leftOut.some((g) => g.screen === id && !g.items);

describe('flow sync coverage', () => {
  it('has a check or a whole-screen leave-out for every flow screen', () => {
    const unchecked = contract.screens
      .filter((s) => !wholeLeftOut(s.id) && !results.some((r) => r.name === s.id))
      .map((s) => s.id);
    expect(unchecked, 'flow screens with no expectFlow case and no leave-out').toEqual([]);
  });

  it('keeps every result in step with the pinned flow', () => {
    for (const { name, result } of results) {
      const screen = contract.screens.find((s) => s.id === result.screen);
      expect(screen, `${name}: not a screen of flow rev ${contract.rev}`).toBeDefined();
      expect(name).toBe(result.state ? `${result.screen}@${result.state}` : result.screen);
      expect(result.hash, `${name}: run npx vitest run -u`).toBe(screen!.hash);
      expect(wholeLeftOut(result.screen), `${name}: checked but left out as a whole`).toBe(false);
      expect(
        results.some((r) => r.name === result.screen),
        `${name}: no base case`
      ).toBe(true);
    }
  });

  it('names only flow screens in flow-gaps.json', () => {
    const ids = new Set(contract.screens.map((s) => s.id));
    const all = [...gaps.leftOut, ...gaps.allowed, ...gaps.drift].map((g) => g.screen);
    expect(all.filter((id) => !ids.has(id))).toEqual([]);
  });

  it('reports every flow screen in /flow-sync.json', () => {
    const report = flowSync(
      contract,
      gaps,
      results.map((r) => r.result),
      'test'
    );
    expect(Object.keys(report.screens)).toEqual(contract.screens.map((s) => s.id));
    const broken = Object.entries(report.screens).filter(([, s]) =>
      s.missing.some((m) => m.includes('screen: '))
    );
    expect(broken).toEqual([]);
  });
});

const SCREEN: ScreenContract = {
  id: 'demo',
  hash: 'h1',
  notesHash: 'n1',
  elements: [
    { k: 'back', text: 'Brunchtime', to: 'show' },
    { k: 'h1', text: 'Demo' },
    { k: 'pill', text: 'Upcoming · Live' },
    { k: 'row', text: 'So 25.10. · Brunchtime', meta: 'Upcoming', to: 'episode' },
    { k: 'field', text: 'Title', to: null },
    { k: 'check', text: 'Record this show', meta: '' },
    { k: 'button', text: 'Go Live', to: 'studio' },
    { k: 'button', text: 'Create guest link', to: 'guest-link' },
    { k: 'text', text: 'Body text is not checked.' },
  ],
};
const NO_GAPS: FlowGaps = { leftOut: [], allowed: [], drift: [] };

function dom(html: string): Element {
  const root = document.createElement('div');
  root.innerHTML = html;
  return root;
}

const PAGE = `
  <a>&lt; Brunchtime</a>
  <h1>Demo</h1>
  <p class="status">Upcoming · Live</p>
  <ul><li><a><span>So 25.10. · Brunchtime</span><span>Upcoming</span></a></li></ul>
  <label><span class="label">Title *</span><input></label>
  <label><input type="checkbox"> Record this show</label>
  <button>Go Live</button>
  <button hidden>Hidden</button>`;

describe('compareScreen', () => {
  it('finds every flow item, written the way the code shows it', () => {
    const { result } = compareScreen(SCREEN, NO_GAPS, dom(PAGE));
    expect(result.missing).toEqual(['button: Create guest link']);
    expect(result.extra).toEqual([]);
    expect(result.status).toBe('drift');
  });

  it('reports items the flow does not have', () => {
    const { result, unexplained } = compareScreen(
      SCREEN,
      NO_GAPS,
      dom(PAGE + '<button>Create guest link</button><button>Reconnect</button><h2>Chat</h2>')
    );
    expect(result.extra).toEqual(['h2: Chat', 'button: Reconnect']);
    expect(unexplained).toEqual(['h2: Chat', 'button: Reconnect']);
  });

  it('keeps left-out items apart and flags a left-out item that is shown', () => {
    const leftOut = {
      leftOut: [{ screen: 'demo', release: 'R5', items: ['button: Create guest link'] }],
      allowed: [],
      drift: [],
    };
    const outcome = compareScreen(SCREEN, leftOut, dom(PAGE));
    expect(outcome.result).toMatchObject({
      status: 'ok',
      missing: [],
      leftOut: [{ item: 'button: Create guest link', release: 'R5' }],
    });
    const shown = compareScreen(SCREEN, leftOut, dom(PAGE + '<button>Create guest link</button>'));
    expect(shown.stale).toEqual(['left out but shown: button: Create guest link']);
  });

  it('lets allowed extras pass and known drift stay drift, per state', () => {
    const page = dom(PAGE + '<button aria-label="Toggle menu">☰</button>');
    const known: FlowGaps = {
      leftOut: [],
      allowed: [{ screen: 'demo', items: ['button: Toggle menu'], why: 'note' }],
      drift: [
        { screen: 'demo', state: 'down', items: ['button: Create guest link'], why: 'ticket' },
      ],
    };
    const base = compareScreen(SCREEN, known, page);
    expect(base.result.extra).toEqual([]);
    expect(base.unexplained).toEqual(['button: Create guest link']);
    expect(base.stale).toEqual([]);

    const down = compareScreen(SCREEN, known, page, 'down');
    expect(down.result).toMatchObject({ state: 'down', status: 'drift' });
    expect(down.unexplained).toEqual(['button: Toggle menu']);
    expect(
      compareScreen(SCREEN, known, dom(PAGE + '<button>Create guest link</button>'), 'down').stale
    ).toEqual(['known drift is gone: button: Create guest link']);
  });

  it('refuses a screen that is left out as a whole', () => {
    const whole = { leftOut: [{ screen: 'demo', release: 'R1' }], allowed: [], drift: [] };
    expect(() => compareScreen(SCREEN, whole, dom(PAGE))).toThrow('left out as a whole screen');
  });
});

describe('flowSync', () => {
  const two = {
    ...contract,
    screens: [
      { ...SCREEN, id: 'a' },
      { ...SCREEN, id: 'b' },
    ],
  };
  const result = (over: Partial<ScreenResult>): ScreenResult => ({
    screen: 'a',
    hash: 'h1',
    status: 'ok',
    leftOut: [],
    missing: [],
    extra: [],
    ...over,
  });

  it('merges a second state into its screen and marks a screen with no check', () => {
    const report = flowSync(
      two,
      NO_GAPS,
      [
        result({}),
        result({ state: 'down', status: 'drift', missing: ['row: Connection | stable'] }),
      ],
      'abc1234'
    );
    expect(report).toMatchObject({ format: 'flow-sync', flow: 'moafunk', build: 'abc1234' });
    expect(report.screens.a).toMatchObject({
      status: 'drift',
      missing: ['[down] row: Connection | stable'],
    });
    expect(report.screens.b).toMatchObject({ status: 'drift', missing: ['screen: no check'] });
  });

  it('reports a whole-screen leave-out with its release', () => {
    const report = flowSync(
      two,
      { leftOut: [{ screen: 'b', release: 'R5' }], allowed: [], drift: [] },
      [result({})],
      'x'
    );
    expect(report.screens.b).toMatchObject({
      status: 'left-out',
      release: 'R5',
      hash: 'h1',
      notesHash: 'n1',
    });
    expect(report.screens.a.status).toBe('ok');
  });

  it('marks a result from an older flow', () => {
    const report = flowSync(two, NO_GAPS, [result({ hash: 'old' }), result({ screen: 'b' })], 'x');
    expect(report.screens.a.missing).toEqual(['screen: checked against an older flow']);
  });
});
