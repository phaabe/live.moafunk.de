# Password recovery

Unexpired Host and Admin accounts can verify a recovery email and reset their
password through a single-use link. Guest and Superadmin accounts continue to
use administrator-assisted recovery. Sign-in still uses the username.

## Enable mail delivery

Production activation is tracked in
https://github.com/phaabe/live.moafunk.de/issues/722, including the detailed
procedure and verification evidence. Anton reported the Resend domain
`live.moafunk.de` verified and the token stored. Secret access, token permissions
and actual delivery still need checking. Preserve the verified DNS records;
use the exact records from this domain's Resend dashboard if repairs are needed.
Disable click and open tracking for recovery mail.

The backend workflow builds images only; it does not deploy the API or generate
its environment file. Activation requires the safe deployment path in
https://github.com/phaabe/live.moafunk.de/issues/381 and Anton's approval of the
prepared production operation. Deploy the recovery code with mail disabled first.
The required B1 `unheard-backend check-config` with strict dotenv parsing and
recovery validation is not implemented on `main` yet. Keep mail off until it is
merged and present in the deployed image.

| Setting                 | Value                                                                                     |
| ----------------------- | ----------------------------------------------------------------------------------------- |
| `RECOVERY_MAIL_API_KEY` | Bitwarden Secrets Manager `RESEND_API_TOKEN`, UUID `f42650f2-11a0-4b0d-9c45-b4e001737980` |
| `RECOVERY_MAIL_FROM`    | `"Moafunk <noreply@live.moafunk.de>"` (keep quotes in the dotenv file)                    |
| `RECOVERY_PUBLIC_URL`   | `https://admin.live.moafunk.de/`                                                          |
| `RECOVERY_TRUST_PROXY`  | Default `false`; set `true` only behind the trusted nginx proxy described below           |

The public URL must use HTTPS and have no credentials, query or fragment. Reset
links use hash routes under this configured URL; request headers never choose
the destination. The backend serves the admin SPA at `/`.

Reuse the existing secret by UUID. Confirm sending-only access to
`live.moafunk.de` and access by the approved Secrets Manager identity. Transfer
the value directly to the approved host configuration process over SSH with
shell tracing off; never print it or put it in arguments or local temporary
files. Do not add it to the monitoring workflow or local development defaults.

Only enable `RECOVERY_TRUST_PROXY` when clients cannot reach the backend directly
and nginx overwrites `X-Real-IP` with `$remote_addr`. Check the actual host rules
and loopback API binding. Otherwise leave proxy trust disabled: clients behind
nginx share one limit of 20 public recovery requests per 15 minutes. Resolve
this before completing production activation.

Under the approved maintenance procedure:

1. Check the deployed digest, unauthenticated `GET /api/auth/recovery` (401) and
   authenticated settings (`enabled: false`). Exclude concurrent configuration
   writers and deployments; complete the procedure's idle and drain checks.
2. Confirm `/opt/unheard-backend/.env` is mounted at `/app/.env`. Check container
   startup environment overrides without printing values. Resolve conflicts
   first: restarting a container does not replace its startup environment.
3. Make a root-only host backup and record hash, inode, ownership and permissions.
   Prepare a restricted candidate on the host, preserving unrelated settings
   and replacing the four settings above so each occurs exactly once.
4. Validate the whole candidate using the required `check-config` from the
   deployed image, with no network or data mount. It must reject malformed
   dotenv input, check recovery settings and redact values. Do not apply a
   candidate with a validation failure or a duplicate warning for these settings.
5. Stop only the API. Write the validated contents into the existing mounted
   file without replacing its inode. Verify hash, inode, owner and permissions;
   start the same container/image. Do not pull an image or restart other services.
   Delete the candidate, which contains the key.
6. Check `/health` and authenticated settings (`enabled: true`), then perform
   the real-mail verification below. Configuration acceptance alone does not
   prove delivery.

Do not append settings: dotenvy uses the first occurrence and preserves process
environment values. Startup currently ignores dotenv parse errors, so a malformed
line may prevent later settings loading. Delivery is disabled only when all
three mail variables are absent; empty, partial or invalid values fail startup.
`RECOVERY_TRUST_PROXY`, if present, must be exactly `true` or `false`.

Provider API: [Resend send email](https://resend.com/docs/api-reference/emails/send-email).

## Verify production delivery

Use an unexpired test Host and a tester-controlled mailbox. Verify the recovery
address, request a reset and sign in by username with the new password. Check
that the old password, old session and reused link fail. Opening either link
alone must not consume it. Confirm password-reset and address removal/replacement
notices arrive, and inspect SPF/DKIM results in a delivered message.

Complete the activation ticket's separate-client IP-limit check and
admin-assisted recovery checks. Test provider failures only in a disposable local
configuration; never break the production key. Record the deployed digest, date
and results without addresses or tokens. Use backend logs and Resend delivery
status; Telegram alert delivery needs its own operational check.

## Delivery and recovery behavior

- Verification and reset links expire after 30 minutes. Opening a link does not
  consume it; the user confirms on the page.
- Password reset signs out existing sessions. An already connected broadcast
  stays connected; new connections must sign in again.
- Removing or replacing an address notifies the previous address. A successful
  password reset also sends a notification.
- Requests have a generic response, including when no eligible account matches.
- Delivery starts after the database transaction commits, with at most eight
  concurrent sends and a ten-second provider timeout. Errors and saturation log
  a sanitized warning and notify the existing Telegram admin channel when configured.
  A separate single permit bounds saturation alerts. Alerts time out after five seconds.
- There is no durable queue or automatic resend. A provider failure, capacity
  limit or backend restart may lose a mail. The user requests a new link;
  operators check configuration and provider status. No address or token is
  included in mail failure logs or alerts.

## Rollback and disable

If activation checks fail, stop only the API, restore backup contents into the
same mounted inode, verify the backup hash and permissions, and start the same
container/image. Verify health and the previous mail-off state. Do not restore
by renaming a replacement file over the bind-mounted file.

To disable delivery later, use the same validated maintenance procedure to
remove every definition of the three mail variables and confirm no startup
environment overrides supply them. `RECOVERY_TRUST_PROXY` may remain. Restart
the API and verify authenticated settings show `enabled: false`. Retain recovery
tables; administrator-assisted password reset remains available.
