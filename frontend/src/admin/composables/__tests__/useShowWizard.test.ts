import { describe, it, expect, beforeEach } from 'vitest';
import { useShowWizard } from '../useShowWizard';

const wizard = useShowWizard();

/** Give the date step a valid start/end window so canProceed passes. */
function setValidDate() {
  wizard.startDateTime.value = new Date(2026, 5, 1, 20, 0);
  wizard.endDateTime.value = new Date(2026, 5, 1, 22, 0);
}

describe('useShowWizard step machine', () => {
  beforeEach(() => {
    wizard.start({ isAdmin: false });
  });

  it('admin "new template" branch walks through to confirm', () => {
    wizard.start({ isAdmin: true });
    expect(wizard.currentStep.value).toBe('choice');

    wizard.setMode('new');
    expect(wizard.steps.value).toEqual([
      'choice',
      'name',
      'cover',
      'description',
      'date',
      'host',
      'stream-mode',
      'confirm',
    ]);

    expect(wizard.goNext()).toBe(true); // → name
    expect(wizard.currentStep.value).toBe('name');

    // Name is required to advance.
    expect(wizard.canProceed.value).toBe(false);
    wizard.newName.value = 'Test Show';
    expect(wizard.goNext()).toBe(true); // → cover (optional)
    expect(wizard.goNext()).toBe(true); // → description (optional)
    expect(wizard.currentStep.value).toBe('description');
    expect(wizard.goNext()).toBe(true); // → date

    expect(wizard.currentStep.value).toBe('date');
    expect(wizard.canProceed.value).toBe(false); // no date yet
    setValidDate();
    expect(wizard.goNext()).toBe(true); // → host

    expect(wizard.currentStep.value).toBe('host');
    expect(wizard.canProceed.value).toBe(false); // no existing user picked yet
    wizard.assigneeUserId.value = 42;
    expect(wizard.goNext()).toBe(true); // → stream-mode

    expect(wizard.currentStep.value).toBe('stream-mode');
    expect(wizard.canProceed.value).toBe(false); // no mode chosen yet
    wizard.setStreamMode('live');
    expect(wizard.goNext()).toBe(true); // → confirm
    expect(wizard.currentStep.value).toBe('confirm');
    expect(wizard.isLastStep.value).toBe(true);
  });

  it('host "existing template" branch includes the unified host step', () => {
    wizard.start({ isAdmin: false });
    wizard.setMode('existing');
    expect(wizard.steps.value).toEqual([
      'choice',
      'select',
      'date',
      'host',
      'stream-mode',
      'confirm',
    ]);

    expect(wizard.goNext()).toBe(true); // → select
    expect(wizard.canProceed.value).toBe(false); // nothing selected
    wizard.selectedTemplateId.value = 7;
    expect(wizard.goNext()).toBe(true); // → date
    setValidDate();
    expect(wizard.goNext()).toBe(true); // → host
    expect(wizard.currentStep.value).toBe('host');
    wizard.assigneeUserId.value = 5;
    expect(wizard.goNext()).toBe(true); // → stream-mode
    wizard.setStreamMode('prerecorded');
    expect(wizard.goNext()).toBe(true); // → confirm
    expect(wizard.currentStep.value).toBe('confirm');
  });

  it('host step accepts a guest username instead of an existing user', () => {
    wizard.start({ isAdmin: false });
    wizard.setMode('existing');
    wizard.goNext(); // → select
    wizard.selectedTemplateId.value = 7;
    wizard.goNext(); // → date
    setValidDate();
    wizard.goNext(); // → host

    expect(wizard.currentStep.value).toBe('host');
    // Guest sub-mode: a non-empty username is enough to proceed.
    wizard.setHostMode('guest');
    expect(wizard.canProceed.value).toBe(false);
    wizard.guestUsername.value = 'dj-guest';
    expect(wizard.canProceed.value).toBe(true);
    expect(wizard.summaryHost.value).toBe('dj-guest (guest)');
  });

  it('only allows direct navigation to already-visited steps', () => {
    wizard.start({ isAdmin: false });
    wizard.setMode('existing');
    wizard.goNext(); // → select (index 1), maxVisited = 1

    // Cannot jump ahead past maxVisited.
    expect(wizard.canNavigateTo(3)).toBe(false);
    expect(wizard.goToStep(3)).toBe(false);
    expect(wizard.stepIndex.value).toBe(1);

    // Can go back to a visited step.
    expect(wizard.canNavigateTo(0)).toBe(true);
    expect(wizard.goToStep(0)).toBe(true);
    expect(wizard.stepIndex.value).toBe(0);
  });

  it('switching branch at the choice step clears forward progress', () => {
    wizard.start({ isAdmin: false });
    wizard.setMode('new');
    wizard.newName.value = 'X';
    wizard.goNext(); // → name (index 1)
    wizard.goNext(); // → cover (index 2), maxVisited = 2

    // Go back to choice and switch branch.
    wizard.goToStep(0);
    wizard.setMode('existing');
    expect(wizard.maxVisited.value).toBe(0);
    expect(wizard.canNavigateTo(2)).toBe(false);
  });

  it('starts with no templates known (choice gate stays closed until loaded)', () => {
    wizard.start({ isAdmin: false });
    expect(wizard.hasTemplates.value).toBe(false);
  });

  it('goToNamedStep jumps back to a visited step from confirm', () => {
    wizard.start({ isAdmin: false });
    wizard.setMode('existing');
    wizard.goNext(); // → select
    wizard.selectedTemplateId.value = 7;
    wizard.goNext(); // → date
    setValidDate();
    wizard.goNext(); // → host
    wizard.assigneeUserId.value = 5;
    wizard.goNext(); // → stream-mode
    wizard.setStreamMode('live');
    wizard.goNext(); // → confirm

    expect(wizard.currentStep.value).toBe('confirm');
    expect(wizard.goToNamedStep('date')).toBe(true);
    expect(wizard.currentStep.value).toBe('date');

    // A step outside the current branch is not navigable.
    expect(wizard.goToNamedStep('name')).toBe(false);
  });
});
