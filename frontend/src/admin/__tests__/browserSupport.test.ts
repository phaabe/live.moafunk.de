import { describe, expect, it } from 'vitest';
import { createApp } from 'vue';
import { browserProblem, detectBrowser } from '../composables/browserSupport';
import BrowserWarning from '../components/BrowserWarning.vue';
import indexHtml from '../index.html?raw';

const UA = {
  chrome:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
  oldChrome:
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/95.0.4638.69 Safari/537.36',
  edge: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0',
  oldFirefox: 'Mozilla/5.0 (X11; Linux x86_64; rv:91.0) Gecko/20100101 Firefox/91.0',
  safari:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15',
  oldSafari:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15',
  chromeOnOldIos:
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/131.0.6778.73 Mobile/15E148 Safari/604.1',
};

describe('detectBrowser', () => {
  it('names the browser and its version', () => {
    expect(detectBrowser(UA.chrome)).toEqual({ name: 'Chrome', version: 131 });
    expect(detectBrowser(UA.edge)).toEqual({ name: 'Edge', version: 131 });
    expect(detectBrowser(UA.oldFirefox)).toEqual({ name: 'Firefox', version: 91 });
    expect(detectBrowser(UA.safari)).toEqual({ name: 'Safari', version: 18.4 });
  });

  it('takes every iOS browser as Safari at the iOS version', () => {
    expect(detectBrowser(UA.chromeOnOldIos)).toEqual({ name: 'Safari', version: 17.5 });
  });

  it('does not guess an unknown browser', () => {
    expect(detectBrowser('curl/8.7.1')).toBeNull();
  });
});

describe('browserProblem', () => {
  it('is null for a browser at or above the floor that can stream', () => {
    for (const ua of [UA.chrome, UA.edge, UA.safari]) expect(browserProblem(ua, true)).toBeNull();
    expect(browserProblem('curl/8.7.1', true)).toBeNull();
  });

  it('names a browser under the floor, also when it could stream', () => {
    expect(browserProblem(UA.oldChrome, true)).toMatch(
      /^Your browser \(Chrome 95\) is too old for streaming\. Update it/
    );
    expect(browserProblem(UA.oldFirefox, true)).toContain('(Firefox 91) is too old');
    expect(browserProblem(UA.oldSafari, false)).toContain('(Safari 17.6) is too old');
    expect(browserProblem(UA.chromeOnOldIos, false)).toContain('(Safari 17.5) is too old');
  });

  it('warns when a current browser lacks what streaming needs', () => {
    expect(browserProblem(UA.chrome, false)).toContain('(Chrome 131) cannot stream');
    expect(browserProblem('curl/8.7.1', false)).toMatch(/^This browser cannot stream\./);
  });
});

describe('BrowserWarning', () => {
  // jsdom has no MediaRecorder, so it is a browser that cannot stream.
  it('shows the problem as an alert that cannot be closed', () => {
    const root = document.createElement('div');
    const app = createApp(BrowserWarning);
    app.mount(root);
    expect(root.querySelector('[role="alert"]')?.textContent).toContain('cannot stream');
    expect(root.querySelector('button')).toBeNull();
    app.unmount();
  });
});

describe('index.html', () => {
  const scripts = [...indexHtml.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  const load = (parsed: (source: string) => boolean) => {
    document.body.innerHTML = '<div id="app"></div>';
    // A script the browser cannot parse does not run; the others still do.
    for (const source of scripts) if (parsed(source)) new Function(source)();
    return document.getElementById('app')!.textContent;
  };

  it('stays empty for the app in a browser that reads modern JavaScript', () => {
    expect(load(() => true)).toBe('');
  });

  it('says the browser is too old when it cannot read modern JavaScript', () => {
    expect(load((source) => !source.includes('?.'))).toContain('Your browser is too old');
  });
});
