/**
 * Cards for the per-show host dashboard's 2×2 grid
 * (docs/stream-rework/show-cockpit-plan.md). Composed by GridShell via named
 * slots on ShowDetailPage's external/brunchtime branch.
 *
 * - IdentityPanel   — metadata: cover · show type · title · description
 * - LiveCard        — phase machine: countdown → on air → wrap-up (download/publish)
 * - ScheduleHostPanel — showtime + assigned host
 * - SocialMediaCard — announcement overview (placeholder until the composer lands)
 */
export { default as IdentityPanel } from './IdentityPanel.vue';
export { default as LiveCard } from './LiveCard.vue';
export { default as ScheduleHostPanel } from './ScheduleHostPanel.vue';
export { default as SocialMediaCard } from './SocialMediaCard.vue';
