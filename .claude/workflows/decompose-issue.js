export const meta = {
  name: 'decompose-issue',
  description: 'Decompose a big task into a parent GitHub issue + labeled sub-issues (drafts only)',
  whenToUse: 'When a task is too large for one ticket and should become a parent + several sub-issues on phaabe/live.moafunk.de',
  phases: [
    { title: 'Scope', detail: 'GitNexus-map the task to functional areas + propose a breakdown' },
    { title: 'Refine', detail: 'one agent per sub-issue: sharpen title/body/labels/acceptance' },
  ],
}

// args: the big task description (string). Pass via Workflow({ args: "..." }).
const TASK = typeof args === 'string' ? args : (args && args.task) || ''
if (!TASK) {
  log('No task provided. Pass the big task description as `args`.')
  return { error: 'missing args (task description)' }
}

const REPO = 'live.moafunk.de'

// Label taxonomy is read live by the agents (`gh label list`); these are the known dimensions.
const LABEL_GUIDE = `
Apply EXACTLY ONE type:: label and the MOST SPECIFIC project:: label, only from \`gh label list\`:
- type::backend          → backend/** (Rust/Axum: handlers, recording, soundcloud, image_overlay, telegram, models)
- type::admin_dashboard  → frontend/src/admin/** (Vue 3 + Pinia admin SPA: pages, composables, components)
- project::Stream | recording | Instagram | Telegram | Soundcloud | ImgGen | Upload | Backup
  | Infrastructure | ExternalShows | Ai | unheard-artist-form | UNHEARD
Add bug / enhancement / documentation / later when fitting. Never invent a label.`

const SUBTASK = {
  type: 'object',
  required: ['title', 'area', 'labels', 'body', 'acceptance'],
  properties: {
    title: { type: 'string', description: 'Conventional-Commit-flavoured, e.g. "feat(stream): add pre-listen player"' },
    area: { type: 'string', description: 'backend | admin_dashboard, and the touched files/cluster' },
    labels: { type: 'array', items: { type: 'string' }, description: 'exact existing labels (one type::, one project::, optional others)' },
    body: { type: 'string', description: 'Context / Scope (GitNexus) / Acceptance criteria / Notes markdown' },
    acceptance: { type: 'array', items: { type: 'string' }, description: 'observable, checkable outcomes' },
  },
}

const BREAKDOWN = {
  type: 'object',
  required: ['parentTitle', 'parentSummary', 'subtasks'],
  properties: {
    parentTitle: { type: 'string' },
    parentSummary: { type: 'string', description: 'why this epic exists + affected areas (from GitNexus)' },
    subtasks: { type: 'array', items: SUBTASK, minItems: 2, maxItems: 8 },
  },
}

phase('Scope')
const breakdown = await agent(
  `Decompose this task for the GitHub repo ${REPO} into a parent issue + 2–8 sub-issues.\n\n` +
    `TASK:\n${TASK}\n\n` +
    `Steps:\n` +
    `1. Use gitnexus_query({query, repo: "${REPO}"}) (and gitnexus_context for key symbols) to find which ` +
    `functional areas/files the work touches. Do NOT grep blindly.\n` +
    `2. Run \`gh label list --limit 60\` to read the EXACT available labels.\n` +
    `3. Split into independently-shippable sub-tasks, each with a Conventional-Commit-style title, the ` +
    `touched area, exact labels, a body (Context / Scope (GitNexus) / Acceptance criteria / Notes), and an ` +
    `acceptance-criteria list.\n${LABEL_GUIDE}\n` +
    `Return the structured breakdown only.`,
  { label: 'scope', phase: 'Scope', schema: BREAKDOWN, agentType: 'Explore' },
)

if (!breakdown || !breakdown.subtasks || !breakdown.subtasks.length) {
  return { error: 'scoping produced no subtasks', breakdown }
}
log(`Proposed ${breakdown.subtasks.length} sub-issues under "${breakdown.parentTitle}"`)

phase('Refine')
const refined = await pipeline(
  breakdown.subtasks,
  (st, _orig, i) =>
    agent(
      `Refine this draft sub-issue for ${REPO} so it is crisp and correctly labeled.\n\n` +
        `DRAFT #${i + 1}:\n${JSON.stringify(st, null, 2)}\n\n` +
        `- Verify every label exists via \`gh label list\` (drop/replace any that don't).\n` +
        `- Ensure exactly one type:: label and one specific project:: label.\n` +
        `- Tighten the title (imperative, scoped) and make acceptance criteria observable.\n` +
        `- Keep the body in the Context / Scope (GitNexus) / Acceptance criteria / Notes format.\n` +
        `${LABEL_GUIDE}\nReturn the corrected sub-issue.`,
      { label: `refine:${i + 1}`, phase: 'Refine', schema: SUBTASK, agentType: 'Explore' },
    ),
)

const subtasks = refined.filter(Boolean)
log(`Refined ${subtasks.length}/${breakdown.subtasks.length} sub-issues`)

// Drafts only — issue creation is a confirmed step in the main loop:
//   1) gh issue create the parent (with a "- [ ] <child title>" checklist),
//   2) gh issue create each child with its labels,
//   3) edit the parent body to reference the created child numbers (- [ ] #N).
return {
  repo: REPO,
  parentTitle: breakdown.parentTitle,
  parentSummary: breakdown.parentSummary,
  subtasks,
  next: 'Present these drafts to the user; on confirmation, create the parent then each child via `gh issue create`, then link children in the parent checklist.',
}
