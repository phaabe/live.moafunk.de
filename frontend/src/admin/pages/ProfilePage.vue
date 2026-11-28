<script setup lang="ts">
// Flow screens `profile`, `profile-edit` and `profile-view` (#1117, flow rev 40):
// the own profile with Edit profile, and another host's profile read-only.
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import BoardIcon from '../components/BoardIcon.vue';
import ImageCropDialog from '../components/ImageCropDialog.vue';
import { profileApi, type Profile, type ProfilePlatform } from '../api';
import { useAuthStore } from '../stores/auth';

const MAX_ABOUT = 500;
const GROUPS: { key: ProfilePlatform['group']; label: string }[] = [
  { key: 'music', label: 'Music' },
  { key: 'social', label: 'Social and video' },
  { key: 'other', label: 'Other' },
];
const SHORT: Record<string, string> = {
  instagram: 'IG',
  soundcloud: 'SC',
  mixcloud: 'MC',
  bandcamp: 'BC',
  ra: 'RA',
  spotify: 'SP',
  applemusic: 'AM',
  beatport: 'BP',
  discogs: 'DG',
  youtube: 'YT',
  tiktok: 'TT',
  facebook: 'FB',
  x: 'X',
  threads: 'TH',
  bluesky: 'BS',
  mastodon: 'MA',
  twitch: 'TW',
  linktree: 'LT',
  website: 'WWW',
};

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

/** Another host's profile: read-only, opened from a host name. */
const viewing = computed(() => (route.params.username as string | undefined) ?? null);
const profile = ref<Profile | null>(null);
const platforms = ref<ProfilePlatform[]>([]);
const loaded = ref(false);
const error = ref('');
const editing = ref(false);
const saving = ref(false);

interface Line {
  id: number;
  platform: string;
  value: string;
  label: string;
}
const name = ref('');
const about = ref('');
const common = ref<Record<string, string>>({});
const lines = ref<Line[]>([]);
let nextId = 1;
/** A new photo, or 'remove'; applied on Save. */
const photoChange = ref<File | 'remove' | null>(null);
const photoPreview = ref<string | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const cropDialog = ref<InstanceType<typeof ImageCropDialog> | null>(null);

const byKey = computed(() => new Map(platforms.value.map((p) => [p.key, p])));
const commonPlatforms = computed(() => platforms.value.filter((p) => p.group === 'common'));
const groups = computed(() =>
  GROUPS.map((g) => ({ ...g, items: platforms.value.filter((p) => p.group === g.key) }))
);

const isAddress = (p: ProfilePlatform | undefined) => p?.prefix === 'https://';
/** The address after https://, as the edit field shows it. */
const bare = (url: string) => url.replace(/^https?:\/\//, '').replace(/\/$/, '');

async function load() {
  loaded.value = false;
  error.value = '';
  editing.value = false;
  try {
    if (viewing.value) {
      if (viewing.value === auth.user?.username) {
        await router.replace('/profile');
        return;
      }
      profile.value = await profileApi.of(viewing.value);
      if (!profile.value) error.value = `No profile for “${viewing.value}”.`;
    } else {
      const own = await profileApi.own();
      profile.value = own.profile;
      platforms.value = own.platforms;
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    loaded.value = true;
  }
}
watch(viewing, load, { immediate: true });

function startEdit() {
  const p = profile.value;
  if (!p) return;
  name.value = p.display_name ?? '';
  about.value = p.about ?? '';
  const rest = [...p.links];
  common.value = Object.fromEntries(
    commonPlatforms.value.map((c) => {
      const at = rest.findIndex((l) => l.platform === c.key);
      return [c.key, at < 0 ? '' : rest.splice(at, 1)[0].value];
    })
  );
  lines.value = rest.map((l) => ({
    id: nextId++,
    platform: l.platform,
    value:
      isAddress(byKey.value.get(l.platform)) && l.platform !== 'custom' ? bare(l.value) : l.value,
    label: l.label ?? '',
  }));
  setPhoto(null);
  error.value = '';
  editing.value = true;
}

function setPhoto(change: File | 'remove' | null) {
  if (photoPreview.value) URL.revokeObjectURL(photoPreview.value);
  photoChange.value = change;
  photoPreview.value = change instanceof File ? URL.createObjectURL(change) : null;
}
onBeforeUnmount(() => setPhoto(null));

async function onFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = '';
  // A photo that is not square opens "Crop photo" first; Cancel keeps the old one.
  const picked = file && (await cropDialog.value?.crop(file));
  if (picked) setPhoto(picked);
}

/** What the photo box shows: the pending change while editing, else the saved photo. */
const photo = computed(() => {
  if (editing.value && photoChange.value === 'remove') return null;
  if (editing.value && photoPreview.value) return { src: photoPreview.value, srcset: undefined };
  const p = profile.value;
  return p?.photo_url || p?.photo_srcset
    ? { src: p.photo_url ?? undefined, srcset: p.photo_srcset ?? undefined }
    : null;
});

function addLine() {
  lines.value.push({ id: nextId++, platform: '', value: '', label: '' });
}

function removeLine(id: number) {
  lines.value = lines.value.filter((l) => l.id !== id);
}

function lineName(line: Line) {
  return line.platform === 'custom'
    ? line.label.trim() || 'custom link'
    : (byKey.value.get(line.platform)?.name ?? 'line');
}

async function save() {
  saving.value = true;
  error.value = '';
  try {
    let saved: Profile | null = null;
    if (photoChange.value instanceof File) saved = await profileApi.uploadPhoto(photoChange.value);
    else if (photoChange.value === 'remove') saved = await profileApi.removePhoto();
    setPhoto(null);
    if (saved && auth.user) auth.user.photo_srcset = saved.photo_srcset;
    profile.value = await profileApi.save({
      display_name: name.value,
      about: about.value,
      links: [
        ...commonPlatforms.value.map((p) => ({
          platform: p.key,
          value: common.value[p.key] ?? '',
        })),
        ...lines.value
          .filter((l) => l.platform)
          .map((l) => ({ platform: l.platform, value: l.value, label: l.label })),
      ],
    });
    editing.value = false;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    saving.value = false;
  }
}

function cancel() {
  setPhoto(null);
  error.value = '';
  editing.value = false;
}

const roleLabel = computed(() => (profile.value?.role === 'host' ? 'Host' : 'Admin'));
</script>

<template>
  <div class="profile-page host-page">
    <button v-if="viewing" type="button" class="back-link" @click="router.back()">
      <BoardIcon name="back" :size="16" />Back
    </button>
    <div class="page-head">
      <h1 class="page-title">{{ editing ? 'Edit profile' : 'Profile' }}</h1>
      <BaseButton v-if="!viewing && !editing && profile" variant="secondary" @click="startEdit"
        ><BoardIcon name="pencil" :size="16" />Edit profile</BaseButton
      >
    </div>
    <p v-if="editing" class="hint">Empty fields stay hidden on your profile.</p>
    <p v-if="error" class="field-error" role="alert">{{ error }}</p>

    <template v-if="profile">
      <section class="board-card">
        <div class="board-card-head">
          <h2 class="board-card-title">About you</h2>
        </div>
        <div class="board-card-body about">
          <div class="photo-col">
            <img
              v-if="photo"
              class="photo"
              :src="photo.src"
              :srcset="photo.srcset"
              sizes="128px"
              :alt="`Photo of ${profile.name}`"
            />
            <div v-else class="photo placeholder" role="img" aria-label="No photo yet"></div>
            <template v-if="editing">
              <div class="photo-actions">
                <BaseButton variant="secondary" @click="fileInput?.click()"
                  ><BoardIcon name="upload" :size="16" />Upload photo</BaseButton
                >
                <BaseButton
                  variant="ghost"
                  :disabled="!photo && photoChange !== 'remove'"
                  @click="setPhoto(profile.photo_srcset ? 'remove' : null)"
                  >Remove</BaseButton
                >
              </div>
              <p class="hint">JPG or PNG. A square photo works best.</p>
              <input
                ref="fileInput"
                type="file"
                accept="image/jpeg,image/png"
                hidden
                @change="onFile"
              />
              <ImageCropDialog ref="cropDialog" title="Crop photo" />
            </template>
          </div>

          <div v-if="!editing" class="facts">
            <p class="name">{{ profile.name }}</p>
            <div class="meta">
              <span class="pill">{{ roleLabel }}</span>
              <router-link
                v-for="s in profile.shows"
                :key="s.slug"
                :to="`/shows/${s.slug}`"
                class="show-link"
                ><span class="k">Show:</span><span>{{ s.name }}</span></router-link
              >
            </div>
            <template v-if="profile.about">
              <h3 class="sub">About me</h3>
              <p class="about-text">{{ profile.about }}</p>
            </template>
          </div>

          <div v-else class="fields">
            <label class="field">
              <span class="field-label">Name</span>
              <input
                v-model="name"
                type="text"
                maxlength="60"
                :placeholder="profile.username"
                autocomplete="name"
              />
            </label>
            <div class="field">
              <div class="field-top">
                <label class="field-label" for="profile-about">About me</label>
                <span class="count">{{ about.length }} / {{ MAX_ABOUT }}</span>
              </div>
              <textarea
                id="profile-about"
                v-model="about"
                rows="4"
                :maxlength="MAX_ABOUT"
                placeholder="Who you are, what you play, what your show is about."
              ></textarea>
            </div>
          </div>
        </div>
      </section>

      <section class="board-card">
        <div class="board-card-head">
          <h2 class="board-card-title">Social media</h2>
        </div>
        <div v-if="!editing" class="board-card-body links-body">
          <ul v-if="profile.links.length" class="links">
            <li v-for="(l, i) in profile.links" :key="i">
              <a :href="l.url" target="_blank" rel="noopener noreferrer" class="link"
                ><span
                  class="tile"
                  :data-short="SHORT[l.platform] ?? 'URL'"
                  aria-hidden="true"
                ></span
                ><span class="link-name">{{ l.name }}</span
                ><span class="link-text">{{ l.text }}</span
                ><BoardIcon name="next" :size="16" class="link-go"
              /></a>
            </li>
          </ul>
          <p v-else class="empty">
            {{ viewing ? 'No links yet.' : 'No links yet. Add them with Edit profile.' }}
          </p>
        </div>

        <div v-else class="board-card-body edit-links">
          <p class="hint">Type only your handle.</p>
          <div v-for="p in commonPlatforms" :key="p.key" class="line">
            <label class="line-name" :for="`profile-${p.key}`">{{ p.name }}</label>
            <div class="affix">
              <span v-if="p.prefix" class="fix">{{ p.prefix }}</span>
              <input
                :id="`profile-${p.key}`"
                v-model="common[p.key]"
                type="text"
                :placeholder="p.hint"
                autocapitalize="off"
                spellcheck="false"
              />
              <span v-if="p.suffix" class="fix after">{{ p.suffix }}</span>
            </div>
            <span class="line-end" aria-hidden="true"></span>
          </div>

          <hr v-if="lines.length" class="divider" />
          <div v-for="line in lines" :key="line.id" class="line">
            <div class="line-name">
              <label class="sr-only" :for="`profile-platform-${line.id}`">Platform</label>
              <select :id="`profile-platform-${line.id}`" v-model="line.platform">
                <option value="" disabled>Choose a platform…</option>
                <optgroup v-for="g in groups" :key="g.key" :label="g.label">
                  <option v-for="p in g.items" :key="p.key" :value="p.key">
                    {{ p.key === 'custom' ? 'Custom…' : p.name }}
                  </option>
                </optgroup>
              </select>
            </div>
            <p v-if="!line.platform" class="hint pick">Pick a platform from the list.</p>
            <div v-else-if="line.platform === 'custom'" class="custom">
              <label class="sr-only" :for="`profile-label-${line.id}`">Link name</label>
              <input
                :id="`profile-label-${line.id}`"
                v-model="line.label"
                type="text"
                maxlength="40"
                placeholder="Link name"
              />
              <label class="sr-only" :for="`profile-url-${line.id}`">Link URL</label>
              <input
                :id="`profile-url-${line.id}`"
                v-model="line.value"
                type="text"
                inputmode="url"
                placeholder="https://…"
                autocapitalize="off"
                spellcheck="false"
              />
            </div>
            <div v-else class="affix">
              <label class="sr-only" :for="`profile-line-${line.id}`">{{
                byKey.get(line.platform)?.name
              }}</label>
              <span v-if="byKey.get(line.platform)?.prefix" class="fix">{{
                byKey.get(line.platform)?.prefix
              }}</span>
              <input
                :id="`profile-line-${line.id}`"
                v-model="line.value"
                type="text"
                :placeholder="byKey.get(line.platform)?.hint"
                autocapitalize="off"
                spellcheck="false"
              />
              <span v-if="byKey.get(line.platform)?.suffix" class="fix after">{{
                byKey.get(line.platform)?.suffix
              }}</span>
            </div>
            <button
              type="button"
              class="remove"
              :aria-label="`Remove ${lineName(line)}`"
              @click="removeLine(line.id)"
            >
              <BoardIcon name="close" :size="14" />
            </button>
          </div>

          <div class="add-row">
            <button type="button" class="add" @click="addLine">+ Add link</button>
            <span class="hint">More platforms, or a custom link.</span>
          </div>
        </div>
      </section>

      <div v-if="editing" class="actions">
        <BaseButton :loading="saving" @click="save">Save</BaseButton>
        <BaseButton variant="secondary" :disabled="saving" @click="cancel">Cancel</BaseButton>
      </div>
    </template>
    <p v-else-if="loaded && !error" class="empty">No profile.</p>
  </div>
</template>

<style scoped>
.profile-page {
  max-width: 800px;
  margin: 0 auto;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

.hint {
  margin: 0;
  font-size: var(--font-size-sm);
  color: #8e8ea4;
}

.page-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-md);
}

/* .board-card-body stacks its children; the photo sits left of the text. */
.about {
  display: flex;
  flex-direction: row;
  flex-wrap: wrap;
  gap: var(--spacing-lg);
  align-items: flex-start;
}

.photo-col {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
  align-items: flex-start;
}

.photo {
  width: 128px;
  height: 128px;
  border-radius: 50%;
  object-fit: cover;
  background: #26263e;
  flex: none;
}

.photo.placeholder {
  background:
    radial-gradient(circle at 50% 40%, #4a4a62 0 17%, transparent 18%),
    radial-gradient(ellipse at 50% 100%, #4a4a62 0 40%, transparent 41%), #26263e;
}

.photo-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.facts,
.fields {
  flex: 1 1 320px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.name {
  margin: 0;
  font: 700 26px var(--font-family);
  color: #fff;
}

.meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--spacing-sm);
}

.pill {
  font: 700 11px var(--font-family);
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: #000;
  background: var(--color-primary);
  border-radius: var(--radius-sm);
  padding: 3px 7px;
}

.show-link {
  display: inline-flex;
  gap: 4px;
  color: #b0b0c4;
  font-size: var(--font-size-sm);
}

.show-link:hover {
  color: #fff;
}

.sub {
  margin: 12px 0 0;
  font: 700 11px var(--font-ui);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #8e8ea4;
}

.about-text {
  margin: 0;
  font-size: var(--font-size-md);
  line-height: 1.6;
  color: #ddd;
  white-space: pre-line;
  overflow-wrap: anywhere;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.field-top {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: var(--spacing-md);
}

.field-label {
  font: 700 11px var(--font-ui);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #b0b0c4;
}

.count {
  font: 12px var(--font-family);
  color: #8e8ea4;
}

input[type='text'],
textarea,
select {
  box-sizing: border-box;
  min-height: 44px;
  padding: 0 12px;
  background: #12122a;
  border: 1px solid #34344c;
  border-radius: var(--radius-lg);
  color: #eee;
  font: 15px var(--font-ui);
}

textarea {
  padding: 10px 12px;
  line-height: 1.5;
  resize: vertical;
}

.links-body {
  padding: 0;
}

.links {
  list-style: none;
  margin: 0;
  padding: 0;
}

.link {
  display: grid;
  grid-template-columns: 40px minmax(0, 1fr) auto;
  grid-template-areas:
    'tile name go'
    'tile text go';
  column-gap: 14px;
  align-items: center;
  padding: 12px var(--spacing-md);
  min-height: 44px;
  border-bottom: 1px solid #1f1f36;
  text-decoration: none;
}

.tile {
  grid-area: tile;
  width: 40px;
  height: 40px;
  border-radius: var(--radius-lg);
  display: flex;
  align-items: center;
  justify-content: center;
  background: #1a1a2e;
  border: 1px solid #34344c;
  color: var(--color-primary);
  font: 700 12px var(--font-family);
}

.tile::before {
  content: attr(data-short);
}

.link-name {
  grid-area: name;
  font: 700 11px var(--font-ui);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #8e8ea4;
}

.link-text {
  grid-area: text;
  font-size: 15px;
  color: #fff;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.link-go {
  grid-area: go;
  color: #b0b0c4;
}

.link:hover .link-go,
.link:hover .link-text {
  color: var(--color-primary);
}

.empty {
  margin: 0;
  padding: var(--spacing-md);
  font-size: var(--font-size-sm);
  color: #8e8ea4;
}

.edit-links {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
}

.line-name {
  flex: 0 0 180px;
  min-width: 0;
  font-size: var(--font-size-sm);
  color: #ddd;
}

.line-name select {
  width: 100%;
}

.affix {
  flex: 1 1 280px;
  min-width: 0;
  display: flex;
  min-height: 44px;
  border: 1px solid #34344c;
  border-radius: var(--radius-lg);
  overflow: hidden;
  background: #12122a;
}

.affix input {
  flex: 1 1 auto;
  min-width: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
}

.fix {
  display: flex;
  align-items: center;
  padding: 0 10px;
  background: #1a1a2e;
  border-right: 1px solid #34344c;
  color: #8e8ea4;
  font: 13px var(--font-family);
  white-space: nowrap;
}

.fix.after {
  border-right: 0;
  border-left: 1px solid #34344c;
}

.custom {
  flex: 1 1 280px;
  min-width: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.custom input:first-of-type {
  flex: 1 1 120px;
  min-width: 0;
}

.custom input:last-of-type {
  flex: 2 1 180px;
  min-width: 0;
}

.pick {
  flex: 1 1 280px;
}

.line-end,
.remove {
  flex: 0 0 44px;
  height: 44px;
}

.remove {
  border-radius: var(--radius-lg);
  background: transparent;
  border: 1px solid #34344c;
  color: #b0b0c4;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.remove:hover {
  color: #fff;
  border-color: #4a4a60;
}

.divider {
  width: 100%;
  border: 0;
  border-top: 1px solid #2a2a42;
  margin: 4px 0;
}

.add-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  padding-top: 4px;
}

.add {
  min-height: 44px;
  padding: 0 18px;
  border-radius: var(--radius-lg);
  background: transparent;
  border: 1px dashed #8f8536;
  color: var(--color-primary);
  font: 700 14px var(--font-family);
  letter-spacing: 0.05em;
  text-transform: uppercase;
  cursor: pointer;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}

@media (max-width: 600px) {
  .line-name {
    flex-basis: 100%;
  }
}
</style>
