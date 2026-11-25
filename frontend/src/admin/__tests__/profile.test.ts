import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, type App } from 'vue';
import { createPinia } from 'pinia';
import { createMemoryHistory, createRouter, RouterView, type Router } from 'vue-router';
import { useAuthStore } from '../stores/auth';
import ProfilePage from '../pages/ProfilePage.vue';
import type { Profile, ProfileInput, ProfilePlatform } from '../api';
import { expectFlow } from '../flow/expectFlow';

const platform = (
  key: string,
  name: string,
  group: ProfilePlatform['group'],
  prefix: string,
  suffix = '',
  at = false
): ProfilePlatform => ({ key, name, group, prefix, suffix, at, hint: 'yourhandle' });

const PLATFORMS: ProfilePlatform[] = [
  platform('instagram', 'Instagram', 'common', 'instagram.com/', '', true),
  platform('soundcloud', 'SoundCloud', 'common', 'soundcloud.com/'),
  platform('mixcloud', 'Mixcloud', 'common', 'mixcloud.com/'),
  platform('bandcamp', 'Bandcamp', 'common', '', '.bandcamp.com'),
  platform('ra', 'Resident Advisor', 'music', 'ra.co/dj/'),
  platform('youtube', 'YouTube', 'social', 'youtube.com/@', '', true),
  platform('website', 'Website', 'other', 'https://'),
  platform('custom', 'Custom', 'other', 'https://'),
];

const link = (platform: string, name: string, value: string, url: string, text: string) => ({
  platform,
  name,
  label: platform === 'custom' ? name : null,
  value,
  url,
  text,
});

/** The flow's example (rev 40): lena's profile. */
const LENA: Profile = {
  username: 'lena',
  display_name: 'Lena K.',
  name: 'Lena K.',
  role: 'host',
  about: 'Sunday brunch sounds: soul, disco and slow house. Every Sunday from 12:00.',
  photo_url: null,
  photo_srcset: null,
  shows: [{ slug: 'brunchtime', name: 'Brunchtime' }],
  links: [
    link(
      'instagram',
      'Instagram',
      'lena.brunch',
      'https://instagram.com/lena.brunch',
      '@lena.brunch'
    ),
    link(
      'soundcloud',
      'SoundCloud',
      'lena-brunch',
      'https://soundcloud.com/lena-brunch',
      'soundcloud.com/lena-brunch'
    ),
    link(
      'bandcamp',
      'Bandcamp',
      'lenabrunch',
      'https://lenabrunch.bandcamp.com',
      'lenabrunch.bandcamp.com'
    ),
    link(
      'website',
      'Website',
      'https://lena.example.org/',
      'https://lena.example.org/',
      'lena.example.org'
    ),
    link(
      'custom',
      'Mixtapes',
      'https://lena.example.org/mixes',
      'https://lena.example.org/mixes',
      'lena.example.org/mixes'
    ),
  ],
};

const JONAS: Profile = {
  username: 'jonas',
  display_name: null,
  name: 'jonas',
  role: 'host',
  about: 'Club records after dark, every Tuesday from 20:00.',
  photo_url: null,
  photo_srcset: null,
  shows: [{ slug: 'night-shift', name: 'Night Shift' }],
  links: [
    link(
      'instagram',
      'Instagram',
      'nightshift.jonas',
      'https://instagram.com/nightshift.jonas',
      '@nightshift.jonas'
    ),
    link(
      'mixcloud',
      'Mixcloud',
      'nightshift',
      'https://mixcloud.com/nightshift',
      'mixcloud.com/nightshift'
    ),
  ],
};

let app: App | undefined;
let root: HTMLDivElement;
let requests: string[];
let saved: ProfileInput | undefined;
let lena: Profile;

beforeEach(() => {
  requests = [];
  saved = undefined;
  lena = JSON.parse(JSON.stringify(LENA));
  root = document.createElement('div');
  document.body.appendChild(root);
  // jsdom has no object URLs; the photo preview needs one.
  URL.createObjectURL = () => 'blob:photo';
  URL.revokeObjectURL = () => {};
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      const method = options?.method ?? 'GET';
      requests.push(`${method} ${url}`);
      if (url === '/api/profile' && method === 'PUT') {
        saved = JSON.parse(options!.body as string);
        lena = { ...lena, display_name: saved!.display_name || null, about: saved!.about || null };
        return Response.json(lena);
      }
      if (url === '/api/profile/photo') {
        lena = {
          ...lena,
          photo_srcset: method === 'POST' ? '/api/img?k=users/1/photo 160w' : null,
        };
        return Response.json(lena);
      }
      switch (url) {
        case '/api/profile':
          return Response.json({ profile: lena, platforms: PLATFORMS });
        case '/api/profiles/jonas':
          return Response.json(JONAS);
        case '/api/profiles/nobody':
          return Response.json({ error: 'No profile' }, { status: 404 });
        default:
          throw new Error(`Unexpected API request: ${url}`);
      }
    })
  );
});

afterEach(() => {
  app?.unmount();
  app = undefined;
  root.remove();
  vi.unstubAllGlobals();
});

async function mount(path: string): Promise<Router> {
  const stub = (text: string) => ({ render: () => h('div', text) });
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/profile', component: ProfilePage },
      { path: '/profile/:username', component: ProfilePage },
      { path: '/shows/:slug', component: stub('Show screen') },
    ],
  });
  const pinia = createPinia();
  app = createApp({ render: () => h(RouterView) });
  app.use(pinia).use(router);
  useAuthStore(pinia).user = { id: 1, username: 'lena', role: 'host' };
  await router.push(path);
  await router.isReady();
  app.mount(root);
  await settle();
  return router;
}

async function settle() {
  for (let i = 0; i < 5; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await nextTick();
  }
}

function button(text: string): HTMLButtonElement {
  const found = [...root.querySelectorAll('button')].find(
    (b) => (b.getAttribute('aria-label') ?? b.textContent?.trim()) === text
  );
  if (!found) throw new Error(`No button "${text}"`);
  return found;
}

async function click(text: string) {
  button(text).click();
  await settle();
}

function type(el: Element | null, value: string, event = 'input') {
  const input = el as HTMLInputElement | HTMLSelectElement;
  input.value = value;
  input.dispatchEvent(new Event(event));
}

const field = (label: string) => {
  const forId = [...root.querySelectorAll('label')]
    .find((l) => l.textContent?.trim() === label)
    ?.getAttribute('for');
  return forId
    ? root.querySelector(`#${forId}`)
    : ([...root.querySelectorAll('label')]
        .find((l) => l.querySelector('.field-label')?.textContent === label)
        ?.querySelector('input') ?? null);
};

describe('Profile', () => {
  it('Profile matches the flow', async () => {
    await mount('/profile');
    await expectFlow('profile', root);
  });

  it('Profile · editing matches the flow', async () => {
    await mount('/profile');
    await click('Edit profile');
    await expectFlow('profile-edit', root);
  });

  it('Profile (other host) matches the flow', async () => {
    await mount('/profile/jonas');
    await expectFlow('profile-view', root);
    expect(root.textContent).not.toContain('Edit profile');
  });

  it('shows only the links that are set, each opening in a new tab', async () => {
    lena.links = lena.links.slice(0, 1);
    await mount('/profile');
    const links = [...root.querySelectorAll('.links a')] as HTMLAnchorElement[];
    expect(links.map((a) => a.href)).toEqual(['https://instagram.com/lena.brunch']);
    expect(links[0].target).toBe('_blank');
    expect(links[0].rel).toBe('noopener noreferrer');
    expect(root.textContent).not.toContain('Mixcloud');
  });

  it('says so when there are no links', async () => {
    lena.links = [];
    await mount('/profile');
    expect(root.textContent).toContain('No links yet. Add them with Edit profile.');
  });

  it('edit form: common fields first, then the added lines, empty ones dropped', async () => {
    await mount('/profile');
    await click('Edit profile');
    expect((field('Instagram') as HTMLInputElement).value).toBe('lena.brunch');
    expect((field('Mixcloud') as HTMLInputElement).value).toBe('');
    // The Website line shows the address without https://.
    expect((field('Website') as HTMLInputElement).value).toBe('lena.example.org');

    type(field('Mixcloud'), 'lena-mixes');
    await click('+ Add link');
    const selects = root.querySelectorAll('select');
    type(selects[selects.length - 1], 'youtube', 'change');
    await settle();
    type(field('YouTube'), 'lenatv');
    await click('+ Add link');
    await click('Remove Mixtapes');
    await click('Save');

    expect(saved).toEqual({
      display_name: 'Lena K.',
      about: LENA.about,
      links: [
        { platform: 'instagram', value: 'lena.brunch' },
        { platform: 'soundcloud', value: 'lena-brunch' },
        { platform: 'mixcloud', value: 'lena-mixes' },
        { platform: 'bandcamp', value: 'lenabrunch' },
        { platform: 'website', value: 'lena.example.org', label: '' },
        { platform: 'youtube', value: 'lenatv', label: '' },
      ],
    });
    // Back to the profile.
    expect(root.querySelector('h1')?.textContent).toBe('Profile');
  });

  it('a platform can be added twice', async () => {
    lena.links = [];
    await mount('/profile');
    await click('Edit profile');
    for (const handle of ['lenatv', 'lenalive']) {
      await click('+ Add link');
      const selects = root.querySelectorAll('select');
      type(selects[selects.length - 1], 'youtube', 'change');
      await settle();
      const inputs = root.querySelectorAll<HTMLInputElement>('input[id^="profile-line-"]');
      type(inputs[inputs.length - 1], handle);
    }
    await click('Save');
    expect(saved?.links.filter((l) => l.platform === 'youtube').map((l) => l.value)).toEqual([
      'lenatv',
      'lenalive',
    ]);
  });

  it('Cancel drops the changes and saves nothing', async () => {
    await mount('/profile');
    await click('Edit profile');
    type(field('Name'), 'Someone else');
    await click('Cancel');
    expect(saved).toBeUndefined();
    expect(root.textContent).toContain('Lena K.');
  });

  it('uploads a new photo on Save and puts it in the nav bar', async () => {
    const router = await mount('/profile');
    await click('Edit profile');
    const input = root.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, 'files', {
      value: [new File(['png'], 'me.png', { type: 'image/png' })],
      configurable: true,
    });
    input.dispatchEvent(new Event('change'));
    await settle();
    expect(requests).not.toContain('POST /api/profile/photo');
    await click('Save');
    expect(requests.slice(-2)).toEqual(['POST /api/profile/photo', 'PUT /api/profile']);
    const auth = useAuthStore();
    expect(auth.user?.photo_srcset).toContain('/api/img');
    expect(router.currentRoute.value.path).toBe('/profile');
  });

  it('the own name in the address opens the own profile', async () => {
    const router = await mount('/profile/lena');
    expect(router.currentRoute.value.path).toBe('/profile');
    expect(root.textContent).toContain('Edit profile');
  });

  it('an unknown host reads as an error', async () => {
    await mount('/profile/nobody');
    expect(root.querySelector('[role="alert"]')?.textContent).toContain('No profile for');
  });
});
