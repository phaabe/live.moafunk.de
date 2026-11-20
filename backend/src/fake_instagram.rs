//! A small fake Instagram Graph API for tests. It records every call and
//! answers like graph.instagram.com, so tests can check what was posted
//! without the network.

use axum::{
    body::Body,
    extract::{Request, State},
    http::{StatusCode, Uri},
    response::{IntoResponse, Response},
    Json,
};
use serde_json::{json, Value};
use std::cell::RefCell;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

thread_local! {
    /// Graph API base of this test thread. The default is a dead local port,
    /// so a test can never reach the real Instagram.
    pub(crate) static API_BASE: RefCell<String> = RefCell::new("http://127.0.0.1:1".to_string());
}

/// One call: `METHOD /path` and its query and form parameters.
#[derive(Debug, Clone)]
pub(crate) struct IgCall {
    pub(crate) route: String,
    pub(crate) params: HashMap<String, String>,
}

impl IgCall {
    pub(crate) fn param(&self, key: &str) -> Option<&str> {
        self.params.get(key).map(String::as_str)
    }

    /// A parameter that holds JSON, like `user_tags` or `collaborators`.
    pub(crate) fn json(&self, key: &str) -> Value {
        serde_json::from_str(self.param(key).unwrap()).unwrap()
    }
}

#[derive(Clone)]
pub(crate) struct FakeInstagram {
    calls: Arc<Mutex<Vec<IgCall>>>,
    /// `(used, total)` of the publishing limit.
    quota: Arc<Mutex<(u32, u32)>>,
    /// The token refresh fails like an expired token.
    refresh_fails: Arc<Mutex<bool>>,
    /// Lifetime of a refreshed token in seconds.
    expires_in: Arc<Mutex<i64>>,
    /// The token refresh fails because the token is younger than 24 h.
    refresh_too_young: Arc<Mutex<bool>>,
    /// `media_publish` fails.
    publish_fails: Arc<Mutex<bool>>,
}

impl FakeInstagram {
    /// Serves a new fake on a free local port and points this thread at it.
    pub(crate) async fn serve() -> Self {
        let fake = Self {
            calls: Arc::default(),
            quota: Arc::new(Mutex::new((3, 100))),
            refresh_fails: Arc::default(),
            expires_in: Arc::new(Mutex::new(60 * 24 * 60 * 60)),
            refresh_too_young: Arc::default(),
            publish_fails: Arc::default(),
        };
        let app = axum::Router::new()
            .fallback(fake_request)
            .with_state(fake.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        API_BASE.with(|b| *b.borrow_mut() = base);
        fake
    }

    pub(crate) fn calls(&self) -> Vec<IgCall> {
        self.calls.lock().unwrap().clone()
    }

    pub(crate) fn routes(&self) -> Vec<String> {
        self.calls().into_iter().map(|c| c.route).collect()
    }

    /// The first call whose route ends with `suffix`.
    pub(crate) fn call(&self, suffix: &str) -> IgCall {
        self.calls()
            .into_iter()
            .find(|c| c.route.ends_with(suffix))
            .unwrap_or_else(|| panic!("no call {suffix}: {:?}", self.routes()))
    }

    pub(crate) fn set_quota(&self, used: u32, total: u32) {
        *self.quota.lock().unwrap() = (used, total);
    }

    pub(crate) fn fail_refresh(&self) {
        *self.refresh_fails.lock().unwrap() = true;
    }

    pub(crate) fn refresh_too_young(&self) {
        *self.refresh_too_young.lock().unwrap() = true;
    }

    pub(crate) fn fail_publish(&self, fail: bool) {
        *self.publish_fails.lock().unwrap() = fail;
    }

    pub(crate) fn set_expires_in(&self, secs: i64) {
        *self.expires_in.lock().unwrap() = secs;
    }
}

async fn fake_request(State(fake): State<FakeInstagram>, uri: Uri, request: Request) -> Response {
    let method = request.method().to_string();
    let path = uri.path().to_string();
    let mut params: HashMap<String, String> =
        serde_urlencoded::from_str(uri.query().unwrap_or_default()).unwrap();
    let body = axum::body::to_bytes(request.into_body(), usize::MAX)
        .await
        .unwrap();
    params.extend(serde_urlencoded::from_bytes::<HashMap<String, String>>(&body).unwrap());
    let call = IgCall {
        route: format!("{method} {path}"),
        params,
    };
    fake.calls.lock().unwrap().push(call.clone());

    let fields = call.param("fields").unwrap_or_default();
    let answer = if path == "/refresh_access_token" {
        if *fake.refresh_too_young.lock().unwrap() {
            // Assumed wording: Meta documents the 24 h rule, not its error.
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": {
                    "message": "The access token must be at least 24 hours old to be refreshed",
                    "type": "OAuthException", "code": 190
                }})),
            )
                .into_response();
        }
        if *fake.refresh_fails.lock().unwrap() {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": {
                    "message": "Error validating access token: Session has expired",
                    "type": "OAuthException", "code": 190
                }})),
            )
                .into_response();
        }
        json!({
            "access_token": format!("refreshed-{}", call.param("access_token").unwrap()),
            "token_type": "bearer",
            "expires_in": *fake.expires_in.lock().unwrap(),
        })
    } else if path.ends_with("/content_publishing_limit") {
        let (used, total) = *fake.quota.lock().unwrap();
        json!({"data": [{"quota_usage": used, "config": {"quota_total": total, "quota_duration": 86400}}]})
    } else if method == "POST" && path.ends_with("/media") {
        json!({"id": "container-1"})
    } else if method == "POST"
        && path.ends_with("/media_publish")
        && *fake.publish_fails.lock().unwrap()
    {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": {
                "message": "The user is not an Instagram Business",
                "type": "OAuthException", "code": 10
            }})),
        )
            .into_response();
    } else if method == "POST" && path.ends_with("/media_publish") {
        json!({"id": "media-1"})
    } else if fields == "status_code" {
        json!({"status_code": "FINISHED", "id": "container-1"})
    } else if fields == "permalink" {
        json!({"permalink": "https://www.instagram.com/p/media-1/", "id": "media-1"})
    } else {
        return Response::builder()
            .status(StatusCode::NOT_FOUND)
            .body(Body::empty())
            .unwrap();
    };
    Json(answer).into_response()
}
