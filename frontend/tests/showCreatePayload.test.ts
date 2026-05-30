import { describe, it, expect } from 'vitest';
import {
  buildShowCreatePayload,
  validateShowCreate,
  type ShowCreateForm,
} from '../src/admin/showCreatePayload';

const baseForm: ShowCreateForm = {
  title: '  My Show  ',
  description: 'desc',
  showType: 'brunchtime',
  date: '2026-06-01',
  startTime: '20:00',
  endTime: '22:00',
};

describe('buildShowCreatePayload', () => {
  it('sends all fields for admins', () => {
    expect(buildShowCreatePayload(true, baseForm)).toEqual({
      title: 'My Show',
      date: '2026-06-01',
      start_time: '20:00',
      show_type: 'brunchtime',
      end_time: '22:00',
      description: 'desc',
    });
  });

  it('sends only title/date/start for hosts (no type, end, description)', () => {
    const payload = buildShowCreatePayload(false, baseForm);
    expect(payload).toEqual({
      title: 'My Show',
      date: '2026-06-01',
      start_time: '20:00',
    });
    expect(payload).not.toHaveProperty('show_type');
    expect(payload).not.toHaveProperty('end_time');
    expect(payload).not.toHaveProperty('description');
  });

  it('trims the title', () => {
    expect(buildShowCreatePayload(false, { ...baseForm, title: '  Spaced  ' }).title).toBe('Spaced');
  });
});

describe('validateShowCreate', () => {
  const ok = { title: 'T', startTime: '20:00', endTime: '22:00', startBeforeEnd: true };

  it('passes for a complete admin form', () => {
    expect(validateShowCreate(true, ok)).toBeNull();
  });

  it('passes for a host with only a start time', () => {
    expect(validateShowCreate(false, { title: 'T', startTime: '20:00', endTime: '', startBeforeEnd: false })).toBeNull();
  });

  it('requires a title', () => {
    expect(validateShowCreate(false, { ...ok, title: '   ' })).toBe('Title is required');
  });

  it('requires a start time', () => {
    expect(validateShowCreate(false, { ...ok, startTime: '' })).toBe('Start date & time is required');
  });

  it('requires an end time for admins only', () => {
    expect(validateShowCreate(true, { ...ok, endTime: '' })).toBe('End date & time is required');
  });

  it('rejects an inverted range for admins', () => {
    expect(validateShowCreate(true, { ...ok, startBeforeEnd: false })).toBe('Start must be before end');
  });
});
