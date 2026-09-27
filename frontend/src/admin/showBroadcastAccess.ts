/** Admins may broadcast any show; other users need the host assignment. */
export function canManageShowBroadcast(
  user: { id: number; role: string } | null,
  show: { host_user_id?: number | null } | null
): boolean {
  return (
    !!user &&
    !!show &&
    (user.role === 'admin' || user.role === 'superadmin' || show.host_user_id === user.id)
  );
}
