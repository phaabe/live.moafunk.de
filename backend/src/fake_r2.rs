//! A small fake R2 for tests: path-style S3 objects in memory, served on a
//! local port. It answers the calls the backend makes: PUT, GET, HEAD, DELETE,
//! CopyObject and multipart uploads.

use crate::AppState;
use axum::{
    body::Bytes,
    extract::State,
    http::{HeaderMap, Method, StatusCode, Uri},
    response::{IntoResponse, Response},
};
use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

/// Parts of an open multipart upload, by part number.
type Parts = BTreeMap<u32, Vec<u8>>;

#[derive(Clone, Default)]
pub(crate) struct FakeR2 {
    /// Objects by `<bucket>/<key>`.
    pub(crate) objects: Arc<Mutex<HashMap<String, Vec<u8>>>>,
    /// While set, every write fails like an R2 that refuses writes.
    refuse_writes: Arc<AtomicBool>,
    /// Writes to this `<bucket>/<key>` fail.
    refused_path: Arc<Mutex<Option<String>>>,
    /// Parts of open multipart uploads, by upload id.
    uploads: Arc<Mutex<HashMap<String, Parts>>>,
    /// Every request as `METHOD <bucket>/<key>`.
    requests: Arc<Mutex<Vec<String>>>,
}

impl FakeR2 {
    /// Serves a new fake R2 on a free local port. Returns it and its endpoint.
    pub(crate) async fn serve() -> (Self, String) {
        let r2 = Self::default();
        // A show's mp3 is larger than axum's default body limit.
        let app = axum::Router::new()
            .fallback(fake_r2_request)
            .layer(axum::extract::DefaultBodyLimit::disable())
            .with_state(r2.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        (r2, endpoint)
    }

    pub(crate) fn refuse_writes(&self, refuse: bool) {
        self.refuse_writes.store(refuse, Ordering::SeqCst);
    }

    /// Writes to `key` in the recordings bucket fail; `None` lifts it.
    pub(crate) fn refuse_writes_to(&self, state: &AppState, key: Option<&str>) {
        *self.refused_path.lock().unwrap() = key.map(|key| path(state, key));
    }

    /// The stored object at `key` in the recordings bucket.
    pub(crate) fn object(&self, state: &AppState, key: &str) -> Option<Vec<u8>> {
        self.object_in(&state.config.r2_bucket_name, key)
    }

    pub(crate) fn object_in(&self, bucket: &str, key: &str) -> Option<Vec<u8>> {
        let path = format!("{bucket}/{key}");
        self.objects.lock().unwrap().get(&path).cloned()
    }

    /// Stores `data` at `key` in the recordings bucket.
    pub(crate) fn insert(&self, state: &AppState, key: &str, data: &[u8]) {
        let path = path(state, key);
        self.objects.lock().unwrap().insert(path, data.to_vec());
    }

    /// Keys in `bucket` that start with `prefix`.
    pub(crate) fn keys_in(&self, bucket: &str, prefix: &str) -> Vec<String> {
        let start = format!("{bucket}/{prefix}");
        let mut keys: Vec<String> = self
            .objects
            .lock()
            .unwrap()
            .keys()
            .filter(|path| path.starts_with(&start))
            .map(|path| path[bucket.len() + 1..].to_string())
            .collect();
        keys.sort();
        keys
    }

    /// How many requests with `method` reached `key` in the recordings bucket.
    pub(crate) fn count(&self, state: &AppState, method: &str, key: &str) -> usize {
        let request = format!("{method} {}", path(state, key));
        let requests = self.requests.lock().unwrap();
        requests.iter().filter(|r| **r == request).count()
    }
}

fn path(state: &AppState, key: &str) -> String {
    format!("{}/{key}", state.config.r2_bucket_name)
}

fn xml(status: StatusCode, body: String) -> Response {
    (status, [("content-type", "application/xml")], body).into_response()
}

fn error(status: StatusCode, code: &str) -> Response {
    xml(
        status,
        format!("<?xml version=\"1.0\"?><Error><Code>{code}</Code><Message>no</Message></Error>"),
    )
}

/// The payload of an `aws-chunked` body: `<hex size>[;ext]\r\n<data>\r\n`
/// frames up to a 0-size frame. Other bodies are returned as they are.
fn payload(headers: &HeaderMap, body: Bytes) -> Vec<u8> {
    let chunked = headers
        .get("content-encoding")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.contains("aws-chunked"));
    if !chunked {
        return body.to_vec();
    }
    let mut out = Vec::new();
    let mut rest = &body[..];
    while let Some(eol) = rest.windows(2).position(|w| w == b"\r\n") {
        let line = std::str::from_utf8(&rest[..eol]).unwrap();
        let size = usize::from_str_radix(line.split(';').next().unwrap(), 16).unwrap();
        if size == 0 {
            break;
        }
        out.extend_from_slice(&rest[eol + 2..eol + 2 + size]);
        rest = &rest[eol + 2 + size + 2..];
    }
    out
}

async fn fake_r2_request(
    State(r2): State<FakeR2>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    // Path style: /<bucket>/<key>
    let key = uri.path().trim_start_matches('/').to_string();
    let query: HashMap<String, String> =
        serde_urlencoded::from_str(uri.query().unwrap_or("")).unwrap();
    r2.requests.lock().unwrap().push(format!("{method} {key}"));

    let write = method == Method::PUT || method == Method::POST;
    let refused = r2.refuse_writes.load(Ordering::SeqCst)
        || r2.refused_path.lock().unwrap().as_deref() == Some(key.as_str());
    if write && refused {
        // 403, not 5xx: the SDK does not retry it, so the test stays fast.
        return error(StatusCode::FORBIDDEN, "AccessDenied");
    }

    // Multipart uploads.
    if method == Method::POST && query.contains_key("uploads") {
        let id = uuid::Uuid::new_v4().to_string();
        r2.uploads
            .lock()
            .unwrap()
            .insert(id.clone(), BTreeMap::new());
        let (bucket, object) = key.split_once('/').unwrap();
        return xml(
            StatusCode::OK,
            format!(
                "<?xml version=\"1.0\"?><InitiateMultipartUploadResult><Bucket>{bucket}</Bucket>\
                 <Key>{object}</Key><UploadId>{id}</UploadId></InitiateMultipartUploadResult>"
            ),
        );
    }
    if let Some(id) = query.get("uploadId") {
        let mut uploads = r2.uploads.lock().unwrap();
        let Some(parts) = uploads.get_mut(id) else {
            return error(StatusCode::NOT_FOUND, "NoSuchUpload");
        };
        return match method {
            Method::PUT => {
                let number: u32 = query["partNumber"].parse().unwrap();
                parts.insert(number, payload(&headers, body));
                (StatusCode::OK, [("etag", format!("\"part{number}\""))]).into_response()
            }
            Method::POST => {
                let data = uploads
                    .remove(id)
                    .unwrap()
                    .into_values()
                    .flatten()
                    .collect();
                r2.objects.lock().unwrap().insert(key.clone(), data);
                let (bucket, object) = key.split_once('/').unwrap();
                xml(
                    StatusCode::OK,
                    format!(
                        "<?xml version=\"1.0\"?><CompleteMultipartUploadResult>\
                         <Bucket>{bucket}</Bucket><Key>{object}</Key><ETag>\"e\"</ETag>\
                         </CompleteMultipartUploadResult>"
                    ),
                )
            }
            Method::DELETE => {
                uploads.remove(id);
                StatusCode::NO_CONTENT.into_response()
            }
            _ => StatusCode::METHOD_NOT_ALLOWED.into_response(),
        };
    }

    let mut objects = r2.objects.lock().unwrap();
    match method {
        Method::PUT => {
            if let Some(source) = headers.get("x-amz-copy-source") {
                let source = source.to_str().unwrap().trim_start_matches('/');
                let Some(data) = objects.get(source).cloned() else {
                    return error(StatusCode::NOT_FOUND, "NoSuchKey");
                };
                objects.insert(key, data);
                return xml(
                    StatusCode::OK,
                    "<?xml version=\"1.0\"?><CopyObjectResult><ETag>\"e\"</ETag>\
                     <LastModified>2026-01-01T00:00:00.000Z</LastModified></CopyObjectResult>"
                        .to_string(),
                );
            }
            objects.insert(key, payload(&headers, body));
            (StatusCode::OK, [("etag", "\"e\"")]).into_response()
        }
        // The server sends no body for HEAD, only its length.
        Method::GET | Method::HEAD => match objects.get(&key) {
            Some(data) => (StatusCode::OK, data.clone()).into_response(),
            None if method == Method::HEAD => StatusCode::NOT_FOUND.into_response(),
            None => error(StatusCode::NOT_FOUND, "NoSuchKey"),
        },
        Method::DELETE => {
            objects.remove(&key);
            StatusCode::NO_CONTENT.into_response()
        }
        _ => StatusCode::METHOD_NOT_ALLOWED.into_response(),
    }
}
