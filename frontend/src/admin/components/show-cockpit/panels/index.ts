/**
 * Shell-agnostic panels for the per-show host dashboard
 * (docs/stream-rework/show-cockpit-plan.md). Rendered today by
 * ShowDetailPage's external/brunchtime branch; the cockpit/tabbed shells
 * (PR 2) compose the same panels.
 *
 * MediaPanel and PromotionPanel already existed as standalone cards with
 * panel-shaped APIs (props + emits, own scoped styles) — they are re-exported
 * under their panel names rather than wrapped. PromotionPanel is a static
 * placeholder until the announcement composer lands (plan PR 4).
 */
export { default as IdentityPanel } from './IdentityPanel.vue';
export { default as ScheduleHostPanel } from './ScheduleHostPanel.vue';
export { default as MediaPanel } from '../../show-detail/ShowMediaCard.vue';
export { default as PromotionPanel } from '../../show-detail/ShowSocialChannels.vue';
