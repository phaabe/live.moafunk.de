import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick, type App } from 'vue';
import ConfigPage from '../pages/ConfigPage.vue';

let app: App | undefined;
let root: HTMLDivElement;
let puts: unknown[];
let failPut: boolean;

beforeEach(() => {
  puts = [];
  failPut = false;
  root = document.createElement('div');
  document.body.appendChild(root);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      const method = options?.method ?? 'GET';
      if (url === '/api/settings/notifications') return Response.json({ enabled: true });
      if (url === '/api/settings/services' && method === 'GET')
        return Response.json({ soundcloud: true, instagram: true });
      if (url === '/api/settings/services' && method === 'PUT') {
        const body = JSON.parse(options!.body as string);
        puts.push(body);
        return failPut ? Response.json({ error: 'nope' }, { status: 500 }) : Response.json(body);
      }
      throw new Error(`Unexpected API request: ${method} ${url}`);
    })
  );
});

afterEach(() => {
  app?.unmount();
  app = undefined;
  root.remove();
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 4; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await nextTick();
  }
}

async function mount() {
  app = createApp(ConfigPage);
  app.mount(root);
  await settle();
}

const box = (label: string) =>
  root.querySelector(`input[aria-label="${label}"]`) as HTMLInputElement;

describe('Config · Connections', () => {
  it('turns SoundCloud off and keeps Instagram on', async () => {
    await mount();
    expect(box('SoundCloud').checked).toBe(true);
    box('SoundCloud').click();
    await settle();
    expect(puts).toEqual([{ soundcloud: false, instagram: true }]);
    expect(box('SoundCloud').checked).toBe(false);
    expect(box('Instagram').checked).toBe(true);
  });

  it('flips back when saving fails', async () => {
    failPut = true;
    vi.spyOn(console, 'error').mockImplementation(() => {});
    await mount();
    box('Instagram').click();
    await settle();
    expect(puts).toEqual([{ soundcloud: true, instagram: false }]);
    expect(box('Instagram').checked).toBe(true);
  });
});
