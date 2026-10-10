# Password recovery

Unexpired Host and Admin accounts can verify a recovery email and reset their
password through a single-use link. Guest and Superadmin accounts continue to
use administrator-assisted recovery. Sign-in still uses the username.

## Enable mail delivery

Delivery is disabled when all three mail settings are absent. Setting only some
of them fails backend startup. No provider account or production secret is
created by this change.

1. Create a Resend sender domain and verify the DNS records Resend supplies,
   including SPF and DKIM. Use a sender on that verified domain.
2. Store the provider API key in the existing Bitwarden Secrets Manager project.
   Do not put it in the repository, a ticket or a terminal command.
3. Add the new secret's reference to the explicit Bitwarden mapping in
   `.github/workflows/backend.yml`, and pass the settings below into its generated
   backend environment. The current workflow does not export them automatically.
   Local development uses `backend/scripts/dev-secrets.sh`; add the corresponding
   secret mapping there when setting up a live development sender. Do not edit
   generated environment files on the server.
4. Deploy with operator approval, then verify an email, request a reset and sign
   in with the new password using a test Host account.

| Setting | Value |
| --- | --- |
| `RECOVERY_MAIL_API_KEY` | Resend API key from Bitwarden |
| `RECOVERY_MAIL_FROM` | Verified sender, such as `Moafunk <account@example.org>` |
| `RECOVERY_PUBLIC_URL` | Trusted admin SPA URL, normally `https://admin.live.moafunk.de/` |
| `RECOVERY_TRUST_PROXY` | Default `false`; set `true` only behind the trusted nginx proxy described below |

The public URL must use HTTPS and have no credentials, query or fragment. Reset
links use hash routes under this configured URL; request headers never choose
the destination. The backend serves the admin SPA at `/`.

Only enable `RECOVERY_TRUST_PROXY` when clients cannot reach the backend directly
and nginx overwrites `X-Real-IP` with the real client address. Otherwise leave it
disabled: rate limits use the connecting peer address. Never trust a client-supplied
forwarded header.

Provider API: [Resend send email](https://resend.com/docs/api-reference/emails/send-email).

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

To disable delivery, remove all three mail settings and restart the backend.
Existing administrator-assisted password reset remains available.
