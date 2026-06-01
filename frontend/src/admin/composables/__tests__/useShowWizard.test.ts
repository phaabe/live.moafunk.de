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

  it('admin "new template" branch walks name → cover → description → date → assign → confirm', () => {
    wizard.start({ isAdmin: true });
    expect(wizard.currentStep.value).toBe('choice');

    wizard.setMode('new');
    expect(wizard.steps.value).toEqual([
      'choice',
      'name',
      'cover',
      'description',
      'date',
      'assign',
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
    expect(wizard.goNext()).toBe(true); // → assign

    expect(wizard.currentStep.value).toBe('assign');
    expect(wizard.canProceed.value).toBe(false); // no assignee yet
    wizard.assigneeUserId.value = 42;
    expect(wizard.goNext()).toBe(true); // → confirm
    expect(wizard.currentStep.value).toBe('confirm');
    expect(wizard.isLastStep.value).toBe(true);
  });

  it('host "existing template" branch has no assign step', () => {
    wizard.start({ isAdmin: false });
    wizard.setMode('existing');
    expect(wizard.steps.value).toEqual(['choice', 'select', 'date', 'confirm']);

    expect(wizard.goNext()).toBe(true); // → select
    expect(wizard.canProceed.value).toBe(false); // nothing selected
    wizard.selectedTemplateId.value = 7;
    expect(wizard.goNext()).toBe(true); // → date
    setValidDate();
    expect(wizard.goNext()).toBe(true); // → confirm
    expect(wizard.currentStep.value).toBe('confirm');
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
});
