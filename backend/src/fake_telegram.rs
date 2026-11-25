//! A small fake Telegram Bot API for tests. It records every call and answers
//! like Telegram does, so tests can check what the bot sent without the
//! network. Both the teloxide `Bot` and the raw `reqwest` helpers in
//! `telegram_notify` talk to it.

use crate::AppState;
use axum::{
    body::Body,
    extract::{FromRequest, Multipart, Request, State},
    http::{header, Uri},
    response::{IntoResponse, Response},
    Json,
};
use serde_json::{json, Value};
use std::cell::RefCell;
use std::sync::{Arc, Mutex};

thread_local! {
    /// Bot API base of the raw helpers in this test thread. The default is a
    /// dead local port, so a test can never reach the real Telegram.
    pub(crate) static API_BASE: RefCell<String> = RefCell::new("http://127.0.0.1:1".to_string());
}

/// The admin group and its MoafunkBot topic in the test config.
pub(crate) const ADMIN_CHAT: i64 = -1001;
pub(crate) const TOPIC: i32 = 26;

/// One Bot API call: the method in lower case and its parameters. Multipart
/// text fields stay strings; a file part becomes `{"file": name, "len": n}`.
#[derive(Debug, Clone)]
pub(crate) struct Call {
    pub(crate) method: String,
    pub(crate) params: Value,
}

impl Call {
    pub(crate) fn str(&self, key: &str) -> Option<&str> {
        self.params.get(key).and_then(Value::as_str)
    }

    pub(crate) fn int(&self, key: &str) -> Option<i64> {
        match self.params.get(key)? {
            Value::Number(n) => n.as_i64(),
            Value::String(s) => s.parse().ok(),
            _ => None,
        }
    }

    pub(crate) fn text(&self) -> &str {
        self.str("text").or(self.str("caption")).unwrap_or_default()
    }

    /// `reply_markup` as JSON, from a JSON body or a multipart string.
    pub(crate) fn markup(&self) -> Value {
        match self.params.get("reply_markup") {
            Some(Value::String(s)) => serde_json::from_str(s).unwrap(),
            Some(markup) => markup.clone(),
            None => Value::Null,
        }
    }

    /// `callback_data` of every inline button, row by row.
    pub(crate) fn buttons(&self) -> Vec<Vec<String>> {
        let markup = self.markup();
        markup["inline_keyboard"]
            .as_array()
            .map(|rows| {
                rows.iter()
                    .map(|row| {
                        row.as_array()
                            .unwrap()
                            .iter()
                            .map(|b| b["callback_data"].as_str().unwrap().to_string())
                            .collect()
                    })
                    .collect()
            })
            .unwrap_or_default()
    }
}

#[derive(Clone, Default)]
pub(crate) struct FakeTelegram {
    calls: Arc<Mutex<Vec<Call>>>,
    /// Calls of this method (lower case) fail like a Telegram 400.
    failing: Arc<Mutex<Option<String>>>,
    /// Bytes served for every file download.
    file: Arc<Mutex<Vec<u8>>>,
}

impl FakeTelegram {
    /// Serves a new fake on a free local port, points the raw helpers of this
    /// thread at it and returns a bot that talks to it.
    pub(crate) async fn serve() -> (Self, teloxide::Bot) {
        let fake = Self::default();
        let app = axum::Router::new()
            .fallback(fake_request)
            .with_state(fake.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        API_BASE.with(|b| *b.borrow_mut() = base.clone());
        let bot = teloxide::Bot::new("test-token").set_api_url(reqwest::Url::parse(&base).unwrap());
        (fake, bot)
    }

    pub(crate) fn calls(&self) -> Vec<Call> {
        self.calls.lock().unwrap().clone()
    }

    pub(crate) fn clear(&self) {
        self.calls.lock().unwrap().clear();
    }

    /// Calls of `method` (lower case), oldest first.
    pub(crate) fn of(&self, method: &str) -> Vec<Call> {
        self.calls()
            .into_iter()
            .filter(|c| c.method == method)
            .collect()
    }

    pub(crate) fn fail(&self, method: Option<&str>) {
        *self.failing.lock().unwrap() = method.map(str::to_string);
    }

    pub(crate) fn serve_file(&self, data: &[u8]) {
        *self.file.lock().unwrap() = data.to_vec();
    }

    /// Waits until `n` calls arrived (for notifications sent from a task).
    pub(crate) async fn wait_for(&self, n: usize) -> Vec<Call> {
        for _ in 0..500 {
            if self.calls.lock().unwrap().len() >= n {
                return self.calls();
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        panic!("expected {n} Telegram calls, got {:?}", self.calls());
    }

    /// Every call that posts a new message. Each must go to the admin
    /// group's MoafunkBot topic, or it lands in the General topic.
    pub(crate) fn assert_all_sends_in_topic(&self) {
        let sends: Vec<Call> = self
            .calls()
            .into_iter()
            .filter(|c| c.method.starts_with("send"))
            .collect();
        assert!(!sends.is_empty(), "no message was sent");
        for call in sends {
            assert_eq!(call.int("chat_id"), Some(ADMIN_CHAT), "{call:?}");
            assert_eq!(
                call.int("message_thread_id"),
                Some(TOPIC as i64),
                "{call:?}"
            );
        }
    }
}

/// A state whose bot and raw helpers talk to a new fake Telegram, with the
/// admin group and topic configured. `overrides` go on top of that config.
pub(crate) async fn telegram_state(
    overrides: Value,
) -> (Arc<AppState>, tempfile::TempDir, FakeTelegram) {
    let mut config = json!({
        "telegram_bot_token": "test-token",
        "telegram_admin_chat_id": ADMIN_CHAT,
        "telegram_topic_id": TOPIC,
    });
    config
        .as_object_mut()
        .unwrap()
        .extend(overrides.as_object().unwrap().clone());
    let (state, temp) = crate::broadcast_tests::state_with(config).await;
    let (fake, bot) = FakeTelegram::serve().await;
    let mut state = Arc::into_inner(state).unwrap();
    state.telegram_bot = Some(bot);
    (Arc::new(state), temp, fake)
}

async fn fake_request(State(fake): State<FakeTelegram>, uri: Uri, request: Request) -> Response {
    if uri.path().starts_with("/file/") {
        return fake.file.lock().unwrap().clone().into_response();
    }
    let method = uri
        .path()
        .rsplit('/')
        .next()
        .unwrap_or_default()
        .to_ascii_lowercase();
    let content_type = request
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or_default()
        .to_string();
    let params = if content_type.starts_with("multipart/form-data") {
        multipart_params(request).await
    } else {
        let body = axum::body::to_bytes(request.into_body(), usize::MAX)
            .await
            .unwrap();
        serde_json::from_slice(&body).unwrap_or(Value::Null)
    };
    let chat_id = Call {
        method: method.clone(),
        params: params.clone(),
    }
    .int("chat_id")
    .unwrap_or(ADMIN_CHAT);

    let message_id = {
        let mut calls = fake.calls.lock().unwrap();
        calls.push(Call {
            method: method.clone(),
            params,
        });
        calls.len() as i64 + 100
    };

    if fake.failing.lock().unwrap().as_deref() == Some(method.as_str()) {
        return Json(json!({
            "ok": false, "error_code": 400, "description": "Bad Request: test failure"
        }))
        .into_response();
    }
    let result = match method.as_str() {
        "answercallbackquery" | "pinchatmessage" | "unpinchatmessage" => json!(true),
        "getfile" => json!({
            "file_id": "file-1", "file_unique_id": "unique-1",
            "file_size": fake.file.lock().unwrap().len(), "file_path": "photos/file-1.jpg"
        }),
        _ => json!({
            "message_id": message_id, "date": 1_700_000_000,
            "chat": {"id": chat_id, "type": "supergroup", "title": "Test", "is_forum": true},
            "text": "ok"
        }),
    };
    Json(json!({"ok": true, "result": result})).into_response()
}

async fn multipart_params(request: Request) -> Value {
    let request = request.map(Body::new);
    let mut form = Multipart::from_request(request, &()).await.unwrap();
    let mut params = serde_json::Map::new();
    while let Some(field) = form.next_field().await.unwrap() {
        let name = field.name().unwrap_or_default().to_string();
        let value = match field.file_name().map(str::to_string) {
            Some(file) => json!({"file": file, "len": field.bytes().await.unwrap().len()}),
            None => json!(field.text().await.unwrap()),
        };
        params.insert(name, value);
    }
    Value::Object(params)
}
