//! Best-effort recovery mail. Tokens are committed before delivery is scheduled.

use std::{sync::Arc, time::Duration};

use reqwest::{Client, Url};
use tokio::sync::Semaphore;

use crate::{recovery::MailData, AppError, AppState, Result};

const MAIL_TIMEOUT: Duration = Duration::from_secs(10);
const ALERT_TIMEOUT: Duration = Duration::from_secs(5);

pub enum MailKind {
    Verify,
    Reset,
    PasswordChanged,
    EmailRemoved,
}

struct Provider {
    client: Client,
    api_key: String,
    from: String,
    public_url: Url,
    endpoint: Url,
}

pub struct RecoveryMailer {
    provider: Option<Provider>,
    slots: Arc<Semaphore>,
    alert_slot: Arc<Semaphore>,
}

impl Default for RecoveryMailer {
    fn default() -> Self {
        Self {
            provider: None,
            slots: Arc::new(Semaphore::new(8)),
            alert_slot: Arc::new(Semaphore::new(1)),
        }
    }
}

impl RecoveryMailer {
    pub fn from_env() -> Result<Self> {
        Self::configured(
            std::env::var("RECOVERY_MAIL_API_KEY").ok(),
            std::env::var("RECOVERY_MAIL_FROM").ok(),
            std::env::var("RECOVERY_PUBLIC_URL").ok(),
        )
    }

    fn configured(key: Option<String>, from: Option<String>, url: Option<String>) -> Result<Self> {
        if key.is_none() && from.is_none() && url.is_none() {
            return Ok(Self::default());
        }
        let (Some(api_key), Some(from), Some(url)) = (key, from, url) else {
            return Err(AppError::Config(
                "Recovery mail needs RECOVERY_MAIL_API_KEY, RECOVERY_MAIL_FROM and RECOVERY_PUBLIC_URL".into(),
            ));
        };
        if api_key.trim().is_empty() || from.trim().is_empty() || from.contains(['\r', '\n']) {
            return Err(AppError::Config(
                "Invalid recovery mail configuration".into(),
            ));
        }
        let public_url = Url::parse(&url).map_err(|_| {
            AppError::Config("RECOVERY_PUBLIC_URL must be a valid HTTPS URL".into())
        })?;
        if public_url.scheme() != "https"
            || public_url.host_str().is_none()
            || !public_url.username().is_empty()
            || public_url.password().is_some()
            || public_url.query().is_some()
            || public_url.fragment().is_some()
        {
            return Err(AppError::Config(
                "RECOVERY_PUBLIC_URL must use HTTPS without credentials, query or fragment".into(),
            ));
        }
        let client = Client::builder()
            .timeout(MAIL_TIMEOUT)
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|_| AppError::Config("Could not initialize recovery mail client".into()))?;
        Ok(Self {
            provider: Some(Provider {
                client,
                api_key,
                from,
                public_url,
                endpoint: Url::parse("https://api.resend.com/emails").expect("fixed provider URL"),
            }),
            ..Self::default()
        })
    }

    pub fn enabled(&self) -> bool {
        self.provider.is_some()
    }

    #[cfg(test)]
    pub(crate) fn for_test(endpoint: &str) -> Self {
        let mut mailer = Self::configured(
            Some("test-key".into()),
            Some("Moafunk <account@example.org>".into()),
            Some("https://admin.example.org/".into()),
        )
        .expect("valid test configuration");
        mailer.provider.as_mut().unwrap().endpoint = Url::parse(endpoint).unwrap();
        mailer
    }

    async fn deliver(
        &self,
        data: &MailData,
        kind: MailKind,
    ) -> std::result::Result<(), &'static str> {
        let Some(provider) = &self.provider else {
            return Err("delivery disabled");
        };
        let (subject, text) = message(provider, data, kind)?;
        let response = provider
            .client
            .post(provider.endpoint.clone())
            .bearer_auth(&provider.api_key)
            .json(&serde_json::json!({
                "from": provider.from,
                "to": [&data.email],
                "subject": subject,
                "text": text,
            }))
            .send()
            .await
            .map_err(|_| "provider request failed or timed out")?;
        if !response.status().is_success() {
            // Provider responses can contain the recipient or other sensitive data.
            return Err("provider rejected delivery");
        }
        Ok(())
    }
}

fn message(
    provider: &Provider,
    data: &MailData,
    kind: MailKind,
) -> std::result::Result<(&'static str, String), &'static str> {
    let username = &data.username;
    match kind {
        MailKind::Verify | MailKind::Reset => {
            let token = data.token.as_deref().ok_or("missing mail token")?;
            let (subject, path, action) = match kind {
                MailKind::Verify => (
                    "Verify your Moafunk recovery email",
                    "/verify-email",
                    "verify this recovery email",
                ),
                _ => (
                    "Reset your Moafunk password",
                    "/reset-password",
                    "reset your password",
                ),
            };
            let query = serde_urlencoded::to_string([("token", token)])
                .map_err(|_| "could not create mail link")?;
            let mut url = provider.public_url.clone();
            url.set_fragment(Some(&format!("{path}?{query}")));
            Ok((subject, format!(
                "Moafunk account: {username}\n\nOpen this link to {action}:\n{url}\n\nThe link expires in 30 minutes and can only be used once. Opening it does not change your account; confirm on the page.\n\nIf you did not request this, ignore this email."
            )))
        }
        MailKind::PasswordChanged => Ok((
            "Your Moafunk password was reset",
            format!("The password for Moafunk account {username} was reset.\n\nYou can now sign in with your username and new password. If this was not you, contact a Moafunk administrator."),
        )),
        MailKind::EmailRemoved => Ok((
            "Your Moafunk recovery email was removed",
            format!("This address is no longer the recovery email for Moafunk account {username}.\n\nIf you did not remove or replace it, contact a Moafunk administrator."),
        )),
    }
}

/// Schedule delivery without holding a database transaction or waiting on a provider.
pub fn send(state: Arc<AppState>, data: MailData, kind: MailKind) {
    if !state.recovery_mail.enabled() {
        return;
    }
    let Ok(permit) = state.recovery_mail.slots.clone().try_acquire_owned() else {
        tracing::warn!("Recovery mail failed: delivery capacity reached");
        // A separate single permit reports saturation without spawning an unbounded queue.
        if let Ok(alert_permit) = state.recovery_mail.alert_slot.clone().try_acquire_owned() {
            tokio::spawn(async move {
                let _permit = alert_permit;
                alert(&state).await;
            });
        }
        return;
    };
    tokio::spawn(async move {
        let _permit = permit;
        if let Err(reason) = state.recovery_mail.deliver(&data, kind).await {
            tracing::warn!(reason, "Recovery mail failed");
            alert(&state).await;
        }
    });
}

async fn alert(state: &Arc<AppState>) {
    let _ = tokio::time::timeout(
        ALERT_TIMEOUT,
        crate::telegram_notify::notify_unconditional(
            state,
            "Recovery email delivery failed. Check the backend mail configuration and provider status. Users may need to request a new link.",
        ),
    )
    .await;
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{extract::State, http::StatusCode, routing::post, Json, Router};
    use tokio::sync::mpsc;

    fn configured() -> RecoveryMailer {
        RecoveryMailer::configured(
            Some("test-key".into()),
            Some("Moafunk <account@example.org>".into()),
            Some("https://admin.example.org/".into()),
        )
        .unwrap()
    }

    fn data() -> MailData {
        MailData {
            email: "host@example.org".into(),
            username: "radio-host".into(),
            token: Some("opaque_test-token".into()),
        }
    }

    #[test]
    fn configuration_requires_complete_values_and_trusted_https_url() {
        assert!(!RecoveryMailer::configured(None, None, None)
            .unwrap()
            .enabled());
        assert!(RecoveryMailer::configured(Some("key".into()), None, None).is_err());
        for url in [
            "http://admin.example.org/",
            "https://user:password@admin.example.org/",
            "https://admin.example.org/#/reset-password",
            "https://admin.example.org/?redirect=elsewhere",
        ] {
            assert!(RecoveryMailer::configured(
                Some("key".into()),
                Some("account@example.org".into()),
                Some(url.into()),
            )
            .is_err());
        }
        assert!(configured().enabled());
    }

    #[tokio::test]
    async fn disabled_delivery_does_not_send() {
        assert_eq!(
            RecoveryMailer::default()
                .deliver(&data(), MailKind::Reset)
                .await,
            Err("delivery disabled")
        );
    }

    #[tokio::test]
    async fn sends_reset_link_and_handles_provider_failure() {
        async fn accept(
            State(sender): State<mpsc::Sender<serde_json::Value>>,
            headers: axum::http::HeaderMap,
            Json(body): Json<serde_json::Value>,
        ) -> StatusCode {
            assert_eq!(headers.get("authorization").unwrap(), "Bearer test-key");
            sender.send(body).await.unwrap();
            StatusCode::OK
        }
        let (sender, mut receiver) = mpsc::channel(1);
        let app = Router::new()
            .route("/send", post(accept))
            .route("/fail", post(|| async { StatusCode::SERVICE_UNAVAILABLE }))
            .with_state(sender);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let mut mailer = configured();
        mailer.provider.as_mut().unwrap().endpoint =
            Url::parse(&format!("http://{address}/send")).unwrap();
        mailer.deliver(&data(), MailKind::Reset).await.unwrap();
        let body = receiver.recv().await.unwrap();
        assert_eq!(body["to"][0], "host@example.org");
        assert_eq!(body["from"], "Moafunk <account@example.org>");
        let text = body["text"].as_str().unwrap();
        assert!(text.contains("radio-host"));
        assert!(text.contains("https://admin.example.org/#/reset-password?token=opaque_test-token"));
        mailer.provider.as_mut().unwrap().endpoint =
            Url::parse(&format!("http://{address}/fail")).unwrap();
        assert_eq!(
            mailer.deliver(&data(), MailKind::Reset).await,
            Err("provider rejected delivery")
        );
        server.abort();
    }
}
