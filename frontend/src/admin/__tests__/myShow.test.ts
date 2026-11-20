import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { createApp, h, nextTick, type App } from 'vue';
import { createPinia } from 'pinia';
import { createMemoryHistory, createRouter, RouterView, type Router } from 'vue-router';
import { useAuthStore } from '../stores/auth';
import HostMyShowsPage from '../pages/HostMyShowsPage.vue';
import { showFacts } from '../components/my-show/showFacts';
import type { Series } from '../api';

// My Show (#1050): read mode, edit mode, the discard prompt and ?edit=<slug>.
// The list itself and the flow check are in hostScreens.test.ts.

const BRUNCHTIME: Series = {
  id: 3,
  slug: 'brunchtime',
  name: 'Brunchtime',
  description: 'Sunday brunch sounds, every week from 12:00.',
  cover_url: null,
  rhythm: { repeat_rule: 'weekly', weekday: 7, start_time: '12:00', end_time: '14:00' },
  rhythm_line: 'Sundays · 12:00–14:00',
  instagram_handle: 'brunchtime',
};
const UNHEARD: Series = {
  id: 4,
  slug: 'unheard',
  name: 'UNHEARD',
  description: 'New artists from the UNHEARD pool, once a month.',
  cover_url: null,
  rhythm: null,
  rhythm_line: null,
  instagram_handle: null,
};

let app: App | undefined;
let root: HTMLDivElement;
let mySeries: Series[];
/** The body of the last PUT /api/series/:slug. */
let put: FormData | undefined;
let putFails: boolean;
let confirm: Mock<[message?: string], boolean>;

beforeEach(() => {
  mySeries = [BRUNCHTIME];
  put = undefined;
  putFails = false;
  root = document.createElement('div');
  document.body.appendChild(root);
  confirm = vi.fn((_message?: string) => false);
  vi.stubGlobal('confirm', confirm);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === '/api/series') return Response.json({ series: mySeries });
      const slug = url.match(/^\/api\/series\/([^/]+)$/)?.[1];
      const show = mySeries.find((s) => s.slug === slug);
      if (show && options?.method === 'PUT') {
        if (putFails) return Response.json({ error: 'Show name is taken' }, { status: 400 });
        put = options.body as FormData;
        const form = Object.fromEntries(put.entries()) as Record<string, string>;
        return Response.json({
          ...show,
          name: form.name,
          description: form.description,
          instagram_handle: form.instagram_handle || null,
        } satisfies Series);
      }
      throw new Error(`Unexpected API request: ${options?.method ?? 'GET'} ${url}`);
    })
  );
});

afterEach(() => {
  app?.unmount();
  app = undefined;
  root.remove();
  vi.unstubAllGlobals();
});

async function mount(path = '/shows'): Promise<Router> {
  const stub = (text: string) => ({ render: () => h('div', text) });
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', redirect: '/shows' },
      { path: '/shows', component: HostMyShowsPage },
      { path: '/shows/new', component: stub('New Show screen') },
      { path: '/shows/:slug', component: stub('Show page') },
      { path: '/shows/:slug/book', component: stub('Book Slot screen') },
      { path: '/dashboard', component: stub('Dashboard screen') },
    ],
  });
  const pinia = createPinia();
  app = createApp({ render: () => h(RouterView) });
  app.use(pinia).use(router);
  useAuthStore(pinia).user = { id: 1, username: 'lena', role: 'host' };
  await router.push(path);
  await router.isReady();
  app.mount(root);
  await vi.waitFor(() => expect(root.querySelectorAll('.my-show-card').length).toBeGreaterThan(0));
  await settle();
  return router;
}

async function settle() {
  for (let i = 0; i < 3; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await nextTick();
  }
}

const cards = () => [...root.querySelectorAll('.my-show-card')];
const card = (name: string) => {
  const found = cards().find((c) => c.getAttribute('aria-label')?.startsWith(name));
  if (!found) throw new Error(`No card "${name}"`);
  return found;
};

function button(text: string, within: Element = root): HTMLButtonElement {
  const found = [...within.querySelectorAll('button')].find((b) => b.textContent?.trim() === text);
  if (!found) throw new Error(`No button "${text}"`);
  return found;
}

const hasButton = (text: string, within: Element = root) =>
  [...within.querySelectorAll('button')].some((b) => b.textContent?.trim() === text);

/** A form field by its label text, inside one card. */
function field(within: Element, label: string): HTMLInputElement | HTMLSelectElement {
  for (const box of within.querySelectorAll('.form-input, .field')) {
    const text = box.querySelector('.label')?.textContent?.trim();
    const control = box.querySelector('input, select, textarea');
    if (text === label && control) return control as HTMLInputElement;
  }
  throw new Error(`No field "${label}"`);
}

async function type(el: HTMLInputElement | HTMLSelectElement, value: string) {
  el.value = value;
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? 'change' : 'input'));
  await nextTick();
}

const facts = (within: Element) =>
  [...within.querySelectorAll('dl > div')].map(
    (d) => `${d.querySelector('dt')?.textContent} | ${d.querySelector('dd')?.textContent}`
  );

describe('My Show read mode', () => {
  it('opens Book Slot for the Show', async () => {
    const router = await mount();
    button('Book Slot').click();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.fullPath).toBe('/shows/brunchtime/book')
    );
  });

  it('opens the Show page from the name', async () => {
    const router = await mount();
    (root.querySelector('.name-link') as HTMLAnchorElement).click();
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/brunchtime'));
  });
});

describe('My Show edit mode', () => {
  it('turns the card into the form, with a bar and without Book Slot', async () => {
    const router = await mount();
    button('Edit').click();
    await vi.waitFor(() => expect(root.querySelector('.my-show-card.editing')).not.toBeNull());
    expect(router.currentRoute.value.fullPath).toBe('/shows?edit=brunchtime');
    const c = card('Brunchtime');
    expect(c.querySelector('.edit-title')?.textContent?.trim()).toBe('Editing show details');
    expect(hasButton('Book Slot')).toBe(false);
    expect(hasButton('Edit')).toBe(false);
    expect(hasButton('Cancel')).toBe(true);
    expect(hasButton('Save')).toBe(true);
    expect(c.querySelector('.read-only')).toBeNull();
    expect((field(c, 'Name') as HTMLInputElement).value).toBe('Brunchtime');
    expect(field(c, 'Description').value).toBe('Sunday brunch sounds, every week from 12:00.');
    expect(field(c, 'Repeats').value).toBe('weekly');
    expect(field(c, 'Day').value).toBe('7');
    expect(field(c, 'Instagram handle').value).toBe('@brunchtime');
    expect(c.querySelector('.static')?.textContent).toBe('lena');
  });

  it('leaves edit mode on Cancel without asking when nothing changed', async () => {
    const router = await mount();
    button('Edit').click();
    await vi.waitFor(() => expect(hasButton('Cancel')).toBe(true));
    button('Cancel').click();
    await vi.waitFor(() => expect(hasButton('Book Slot')).toBe(true));
    expect(confirm).not.toHaveBeenCalled();
    expect(router.currentRoute.value.fullPath).toBe('/shows');
  });

  it('asks once before Cancel drops changes', async () => {
    await mount();
    button('Edit').click();
    await vi.waitFor(() => expect(hasButton('Cancel')).toBe(true));
    await type(field(card('Brunchtime'), 'Name'), 'Brunch Club');

    button('Cancel').click();
    await settle();
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(confirm).toHaveBeenCalledWith('Discard changes?');
    // Declined: still editing, the change is kept.
    expect((field(card('Brunchtime'), 'Name') as HTMLInputElement).value).toBe('Brunch Club');

    confirm.mockReturnValue(true);
    button('Cancel').click();
    await vi.waitFor(() => expect(hasButton('Book Slot')).toBe(true));
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(card('Brunchtime').querySelector('h2')?.textContent?.trim()).toBe('Brunchtime');
    expect(put).toBeUndefined();
  });

  it('saves the form and shows the saved details in read mode', async () => {
    const router = await mount();
    button('Edit').click();
    await vi.waitFor(() => expect(hasButton('Save')).toBe(true));
    const c = card('Brunchtime');
    await type(field(c, 'Name'), 'Brunch Club');
    await type(field(c, 'Instagram handle'), '@brunch.club');
    button('Save').click();

    await vi.waitFor(() => expect(hasButton('Book Slot')).toBe(true));
    expect(confirm).not.toHaveBeenCalled();
    expect(router.currentRoute.value.fullPath).toBe('/shows');
    expect(Object.fromEntries(put!.entries())).toEqual({
      name: 'Brunch Club',
      description: 'Sunday brunch sounds, every week from 12:00.',
      repeat_rule: 'weekly',
      weekday: '7',
      start_time: '12:00',
      end_time: '14:00',
      instagram_handle: 'brunch.club',
    });
    const saved = card('Brunch Club');
    expect(saved.querySelector('h2')?.textContent?.trim()).toBe('Brunch Club');
    expect(facts(saved)).toContain('Instagram | @brunch.club');
  });

  it('keeps the form open and says why when Save fails', async () => {
    putFails = true;
    await mount();
    button('Edit').click();
    await vi.waitFor(() => expect(hasButton('Save')).toBe(true));
    await type(field(card('Brunchtime'), 'Name'), 'Brunch Club');
    button('Save').click();
    await vi.waitFor(() =>
      expect(root.querySelector('[role="alert"]')?.textContent).toBe('Show name is taken')
    );
    expect(root.querySelector('.my-show-card.editing')).not.toBeNull();
  });

  it('cannot save a bad Instagram handle or an empty name', async () => {
    await mount();
    button('Edit').click();
    await vi.waitFor(() => expect(hasButton('Save')).toBe(true));
    const c = card('Brunchtime');
    await type(field(c, 'Instagram handle'), '@not a handle');
    expect(button('Save').disabled).toBe(true);
    expect(c.textContent).toContain('Use up to 30 letters, numbers, dots or underscores.');
    await type(field(c, 'Instagram handle'), '@brunchtime');
    expect(button('Save').disabled).toBe(false);
    await type(field(c, 'Name'), ' ');
    expect(button('Save').disabled).toBe(true);
  });

  it('asks before leaving the page with unsaved changes', async () => {
    const router = await mount();
    button('Edit').click();
    await vi.waitFor(() => expect(hasButton('Save')).toBe(true));
    await type(field(card('Brunchtime'), 'Description'), 'Brunch, but later.');
    await router.push('/dashboard');
    expect(confirm).toHaveBeenCalledWith('Discard changes?');
    expect(router.currentRoute.value.fullPath).toBe('/shows?edit=brunchtime');
  });

  it('does not count an edit back to the saved text as a change', async () => {
    await mount();
    button('Edit').click();
    await vi.waitFor(() => expect(hasButton('Cancel')).toBe(true));
    const name = field(card('Brunchtime'), 'Name');
    await type(name, 'Brunch Club');
    await type(name, 'Brunchtime');
    button('Cancel').click();
    await vi.waitFor(() => expect(hasButton('Book Slot')).toBe(true));
    expect(confirm).not.toHaveBeenCalled();
  });
});

describe('My Show ?edit=<slug>', () => {
  it('opens that Show’s card in edit mode', async () => {
    mySeries = [BRUNCHTIME, UNHEARD];
    await mount('/shows?edit=unheard');
    await vi.waitFor(() => expect(root.querySelector('.my-show-card.editing')).not.toBeNull());
    expect(card('UNHEARD').classList.contains('editing')).toBe(true);
    expect(card('Brunchtime').classList.contains('editing')).toBe(false);
    expect(field(card('UNHEARD'), 'Repeats').value).toBe('none');
  });

  it('drops an edit link to a Show that is not the host’s', async () => {
    const router = await mount('/shows?edit=night-shift');
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows'));
    expect(root.querySelector('.my-show-card.editing')).toBeNull();
  });
});

describe('My Show grid', () => {
  it('edits only one card; the others stay in read mode', async () => {
    mySeries = [BRUNCHTIME, UNHEARD];
    await mount();
    expect(cards()).toHaveLength(2);
    button('Edit', card('UNHEARD')).click();
    await vi.waitFor(() => expect(card('UNHEARD').classList.contains('editing')).toBe(true));
    const other = card('Brunchtime');
    expect(other.classList.contains('editing')).toBe(false);
    expect(hasButton('Book Slot', other)).toBe(true);
    expect(other.querySelectorAll('input, select, textarea')).toHaveLength(0);
  });

  it('asks before switching to another card with unsaved changes', async () => {
    mySeries = [BRUNCHTIME, UNHEARD];
    const router = await mount();
    button('Edit', card('UNHEARD')).click();
    await vi.waitFor(() => expect(card('UNHEARD').classList.contains('editing')).toBe(true));
    await type(field(card('UNHEARD'), 'Name'), 'UNHEARD live');

    button('Edit', card('Brunchtime')).click();
    await settle();
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(router.currentRoute.value.fullPath).toBe('/shows?edit=unheard');

    confirm.mockReturnValue(true);
    button('Edit', card('Brunchtime')).click();
    await vi.waitFor(() => expect(card('Brunchtime').classList.contains('editing')).toBe(true));
    expect(card('UNHEARD').classList.contains('editing')).toBe(false);
    expect(card('UNHEARD').querySelector('h2')?.textContent?.trim()).toBe('UNHEARD');
  });
});

describe('showFacts', () => {
  it('has no Day and no Time without a fixed rhythm', () => {
    expect(showFacts(UNHEARD, ['lena'])).toEqual([
      { label: 'Repeats', value: 'No fixed rhythm' },
      { label: 'Instagram', value: 'Not set' },
      { label: 'Hosts', value: 'lena' },
    ]);
  });

  it('lists every host', () => {
    const biweekly: Series = {
      ...BRUNCHTIME,
      rhythm: { ...BRUNCHTIME.rhythm!, repeat_rule: 'biweekly', weekday: 5 },
    };
    expect(showFacts(biweekly, ['lena', 'jonas'], true)).toEqual([
      { label: 'Repeats', value: 'Every 2 weeks' },
      { label: 'Day · Time', value: 'Friday · 12:00–14:00' },
      { label: 'Instagram', value: '@brunchtime' },
      { label: 'Hosts', value: 'lena, jonas' },
    ]);
  });
});
