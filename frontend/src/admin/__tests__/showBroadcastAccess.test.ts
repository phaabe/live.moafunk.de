import { describe, expect, it } from 'vitest';
import { canManageShowBroadcast } from '../showBroadcastAccess';

describe('show broadcast access', () => {
  it.each(['admin', 'superadmin'])(
    'allows %s to broadcast another host’s show or an unassigned show',
    (role) => {
      const user = { id: 1, role };
      expect(canManageShowBroadcast(user, { host_user_id: 7 })).toBe(true);
      expect(canManageShowBroadcast(user, {})).toBe(true);
    }
  );

  it('keeps hosts restricted to their assignment', () => {
    const user = { id: 7, role: 'host' };
    expect(canManageShowBroadcast(user, { host_user_id: 7 })).toBe(true);
    expect(canManageShowBroadcast(user, { host_user_id: 8 })).toBe(false);
    expect(canManageShowBroadcast(user, {})).toBe(false);
  });

  it('denies access without a user or show', () => {
    expect(canManageShowBroadcast(null, {})).toBe(false);
    expect(canManageShowBroadcast({ id: 1, role: 'admin' }, null)).toBe(false);
  });
});
