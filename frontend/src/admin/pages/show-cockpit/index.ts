/**
 * The two dashboard shells under A/B (docs/stream-rework/show-cockpit-plan.md).
 * Both consume the same named slots (banner, identity, schedule, media,
 * promotion, wrapup) + a `phase` prop; ShowDetailPage picks between them via
 * the `?layout=` query. One is deleted once the A/B is decided (plan PR 6).
 */
export { default as CockpitShell } from './CockpitShell.vue';
export { default as TabbedShell } from './TabbedShell.vue';

export type ShellLayout = 'cockpit' | 'tabs';
