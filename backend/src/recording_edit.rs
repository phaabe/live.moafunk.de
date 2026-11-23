//! The recording editor: after a show the host cuts start and end, sets the
//! volume of the whole set and of sections, and places snippets. "Apply edit"
//! renders a new mp3 with ffmpeg from the original recording; the original is
//! never changed. The rendered file is then what the SoundCloud upload sends.
//!
//! The waveform comes from peaks the server computes once per recording and
//! caches in R2, so the browser never decodes a 2 h set.

use std::collections::{HashMap, HashSet};
use std::path::{Path as FsPath, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use axum::{
    body::Bytes,
    extract::{Path, State},
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sqlx::SqlitePool;
use tokio::io::AsyncReadExt;
use tokio::process::Command;

use crate::handlers::api::require_show_editor;
use crate::{models, storage, AppError, AppState, Result};

/// Volume ramp at each edge of a section, in seconds.
const RAMP: f64 = 0.3;
/// Fade at the start of the edited file, in seconds.
const FADE_IN: f64 = 0.5;
/// Fade at the end of the edited file, in seconds.
const FADE_OUT: f64 = 2.0;

const MIN_LENGTH: f64 = 2.5;
const MAX_GAIN_DB: f64 = 12.0;
const MAX_SECTIONS: usize = 20;
const MIN_SECTION: f64 = 0.6;
const MAX_SNIPPETS: usize = 3;
const SNIPPET_LENGTH: (f64, f64) = (5.0, 30.0);
/// Seconds the browser computed (e.g. `end - 30`) may miss a bound by a float error.
const EPS: f64 = 1e-6;
/// The stored duration comes from the capture, not from the decoded mp3; the two
/// differ by encoder padding. An edit that ends this little past it is fine.
const DURATION_SLACK: f64 = 0.5;

/// Peaks: decode rate and samples per value (20 values per second).
const PEAK_RATE: u32 = 8000;
const PEAK_BUCKET: usize = 400;
const PEAKS_PER_SECOND: u32 = PEAK_RATE / PEAK_BUCKET as u32;

/// A 2 h set renders in a few minutes; this only stops a hung ffmpeg.
const FFMPEG_TIMEOUT: Duration = Duration::from_secs(15 * 60);

/// Peak jobs by peaks key: `None` while one runs, `Some(error)` after it failed.
pub type PeaksJobs = std::sync::Mutex<HashMap<String, Option<String>>>;

// ============================================================================
// Edit
// ============================================================================

/// What the host set. All times are seconds from the start of the original file.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RecordingEdit {
    pub start: f64,
    pub end: f64,
    pub gain_db: f64,
    #[serde(default)]
    pub sections: Vec<Section>,
    #[serde(default)]
    pub snippets: Vec<Snippet>,
}

/// A louder or quieter part of the set.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Section {
    pub id: String,
    pub start: f64,
    pub end: f64,
    pub gain_db: f64,
}

/// A part for a later Instagram post. Only stored here.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Snippet {
    pub slot: i64,
    pub start: f64,
    pub length: f64,
}

/// Every rule the edit breaks, empty when it is valid. `duration` is the
/// length of the original file in seconds, when known.
pub fn validate(edit: &RecordingEdit, duration: Option<f64>) -> Vec<String> {
    let mut problems = Vec::new();
    let gain_ok = |db: f64| (-MAX_GAIN_DB..=MAX_GAIN_DB).contains(&db);

    if edit.start < 0.0 {
        problems.push("start must be 0 or more".to_string());
    }
    if edit.start >= edit.end {
        problems.push("start must be before end".to_string());
    }
    if let Some(duration) = duration {
        if edit.end > duration + DURATION_SLACK {
            problems.push(format!(
                "end must not be after the end of the recording ({duration:.1} s)"
            ));
        }
    }
    if edit.end - edit.start < MIN_LENGTH - EPS {
        problems.push(format!("The edit must be at least {MIN_LENGTH} s long"));
    }
    if !gain_ok(edit.gain_db) {
        problems.push(format!(
            "gainDb must be between -{MAX_GAIN_DB} and {MAX_GAIN_DB}"
        ));
    }

    if edit.sections.len() > MAX_SECTIONS {
        problems.push(format!("At most {MAX_SECTIONS} volume sections"));
    }
    for s in &edit.sections {
        if s.end - s.start < MIN_SECTION - EPS {
            problems.push(format!(
                "Section {} must be at least {MIN_SECTION} s long",
                s.id
            ));
        }
        if s.start < edit.start - EPS || s.end > edit.end + EPS {
            problems.push(format!("Section {} must lie inside the edit", s.id));
        }
        if !gain_ok(s.gain_db) {
            problems.push(format!(
                "Section {}: gainDb must be between -{MAX_GAIN_DB} and {MAX_GAIN_DB}",
                s.id
            ));
        }
    }
    let mut sorted: Vec<&Section> = edit.sections.iter().collect();
    sorted.sort_by(|a, b| a.start.total_cmp(&b.start));
    for pair in sorted.windows(2) {
        if pair[1].start < pair[0].end - EPS {
            problems.push(format!(
                "Sections {} and {} overlap",
                pair[0].id, pair[1].id
            ));
        }
    }

    if edit.snippets.len() > MAX_SNIPPETS {
        problems.push(format!("At most {MAX_SNIPPETS} snippets"));
    }
    let mut slots = HashSet::new();
    for s in &edit.snippets {
        if !(1..=MAX_SNIPPETS as i64).contains(&s.slot) {
            problems.push(format!("Snippet slot {} must be 1, 2 or 3", s.slot));
        } else if !slots.insert(s.slot) {
            problems.push(format!("Snippet slot {} is used twice", s.slot));
        }
        let (min, max) = SNIPPET_LENGTH;
        if s.length < min - EPS || s.length > max + EPS {
            problems.push(format!("Snippet {} must be {min} to {max} s long", s.slot));
        }
        if s.start < edit.start - EPS || s.start + s.length > edit.end + EPS {
            problems.push(format!("Snippet {} must lie inside the edit", s.slot));
        }
    }
    problems
}

/// The ffmpeg `-af` filter for `edit`. Must match the browser preview
/// (`renderFilter` in the frontend) character for character.
pub fn render_filter(edit: &RecordingEdit) -> String {
    let sec = |x: f64| format!("{x:.3}");
    let length = edit.end - edit.start;
    let mut parts = vec![
        format!("atrim=start={}:end={}", sec(edit.start), sec(edit.end)),
        "asetpts=PTS-STARTPTS".to_string(),
        format!("volume={}dB", edit.gain_db),
    ];
    if !edit.sections.is_empty() {
        let terms: Vec<String> = edit
            .sections
            .iter()
            .map(|s| {
                let a = sec(s.start - edit.start);
                let b = sec(s.end - edit.start);
                let g = 10f64.powf(s.gain_db / 20.0) - 1.0;
                format!("{g:.5}*clip(min(t-{a}\\,{b}-t)/{RAMP}\\,0\\,1)")
            })
            .collect();
        parts.push(format!("volume=eval=frame:volume='1+{}'", terms.join("+")));
    }
    parts.push(format!("afade=t=in:d={FADE_IN}"));
    parts.push(format!(
        "afade=t=out:st={}:d={FADE_OUT}",
        sec(length - FADE_OUT)
    ));
    parts.push("alimiter=limit=0.97:level=0".to_string());
    parts.join(",")
}

// ============================================================================
// Source recording
// ============================================================================

/// An audio file in R2.
#[derive(Debug, Clone, PartialEq)]
pub struct RecordingSource {
    pub bucket: String,
    pub key: String,
    /// File name for the SoundCloud upload.
    pub filename: String,
    /// Length from `recording_versions.duration_ms`, when known.
    pub duration_ms: Option<i64>,
}

impl RecordingSource {
    fn duration(&self) -> Option<f64> {
        self.duration_ms.map(|ms| ms as f64 / 1000.0)
    }
}

/// The two files of an episode. Each has its own edit.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Source {
    /// Recorded on air: the latest finalized take, else the archive mp3.
    Show,
    /// The file the host uploaded (`shows.recording_key`).
    Own,
}

impl Source {
    pub fn as_str(self) -> &'static str {
        match self {
            Source::Show => "show",
            Source::Own => "own",
        }
    }

    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "show" => Some(Source::Show),
            "own" => Some(Source::Own),
            _ => None,
        }
    }
}

/// The file the host picked for SoundCloud. Not picked yet: the own file
/// when there is one, else the show recording (what older shows did).
pub fn picked_source(show: &models::Show) -> Source {
    match show.recording_source.as_deref().and_then(Source::parse) {
        Some(source) => source,
        None if show.recording_key.is_some() => Source::Own,
        None => Source::Show,
    }
}

/// One file of the show, `None` when it does not exist:
///   - own: the uploaded file (`recording_key`, default bucket),
///   - show: the latest *finalized* live recording (final.mp3, default bucket),
///     else the latest streamed *archive* MP3 (shows bucket) — streamed shows
///     stay `raw` with only an archive_key.
pub async fn resolve_file(
    state: &Arc<AppState>,
    show: &models::Show,
    source: Source,
) -> Result<Option<RecordingSource>> {
    if source == Source::Own {
        return Ok(show.recording_key.as_ref().map(|key| RecordingSource {
            bucket: state.config.r2_bucket_name.clone(),
            key: key.clone(),
            filename: show
                .recording_filename
                .clone()
                .unwrap_or_else(|| "recording.mp3".to_string()),
            duration_ms: None,
        }));
    }
    if let Some(version) =
        crate::db::get_latest_finalized_recording_version(&state.db, show.id).await?
    {
        let final_key = version.final_key.clone().ok_or_else(|| {
            AppError::BadRequest("Finalized recording has no file to upload".to_string())
        })?;
        return Ok(Some(RecordingSource {
            bucket: state.config.r2_bucket_name.clone(),
            key: final_key,
            filename: format!("{}.mp3", version.version),
            duration_ms: version.duration_ms,
        }));
    }
    if let Some(version) =
        crate::db::get_latest_archived_recording_version(&state.db, show.id).await?
    {
        let archive_key = version.archive_key.clone().ok_or_else(|| {
            AppError::BadRequest("Archived recording has no file to upload".to_string())
        })?;
        return Ok(Some(RecordingSource {
            bucket: state.config.r2_shows_bucket_name.clone(),
            key: archive_key,
            filename: format!("{}.mp3", version.version),
            duration_ms: version.duration_ms,
        }));
    }
    Ok(None)
}

/// The picked file and which one it is.
async fn picked_file(
    state: &Arc<AppState>,
    show: &models::Show,
) -> Result<(Source, Option<RecordingSource>)> {
    let source = picked_source(show);
    Ok((source, resolve_file(state, show, source).await?))
}

/// What "Upload to SoundCloud" sends.
#[derive(Debug, Clone, PartialEq)]
pub struct Upload {
    pub file: RecordingSource,
    /// The picked file `file` comes from.
    pub source: Source,
    /// True when `file` is the rendered edit.
    pub edited: bool,
}

/// The picked file's rendered edit when it was made from the current file,
/// else the picked file.
pub async fn upload_source(state: &Arc<AppState>, show: &models::Show) -> Result<Option<Upload>> {
    let (source, Some(original)) = picked_file(state, show).await? else {
        return Ok(None);
    };
    let row = load_row(&state.db, show.id, source).await?;
    if let Some(key) = row.as_ref().and_then(|r| r.render_of(&original)) {
        let stem = FsPath::new(&original.filename)
            .file_stem()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_else(|| "recording".to_string());
        let file = RecordingSource {
            bucket: state.config.r2_bucket_name.clone(),
            key: key.to_string(),
            filename: format!("{stem}-edited.mp3"),
            duration_ms: None,
        };
        return Ok(Some(Upload {
            file,
            source,
            edited: true,
        }));
    }
    Ok(Some(Upload {
        file: original,
        source,
        edited: false,
    }))
}

/// Peaks of `source_key`, cached in the default bucket. Not `.peaks.json`: a
/// manual upload already keeps the browser's 500-value peaks there.
fn peaks_key(source_key: &str) -> String {
    let base = source_key
        .rsplit_once('.')
        .map(|(base, _)| base)
        .unwrap_or(source_key);
    format!("{base}.editor-peaks.json")
}

// ============================================================================
// Stored edit
// ============================================================================

#[derive(Debug, sqlx::FromRow)]
struct EditRow {
    edit_json: String,
    status: String,
    error_message: Option<String>,
    edited_key: Option<String>,
    /// The original the rendered file was made from.
    source_key: Option<String>,
    edited_seconds: Option<f64>,
    rendered_at: Option<String>,
}

impl EditRow {
    /// The rendered file, when it was made from `source` (a new recording makes it stale).
    fn render_of(&self, source: &RecordingSource) -> Option<&str> {
        self.edited_key
            .as_deref()
            .filter(|_| self.source_key.as_deref() == Some(source.key.as_str()))
    }
}

async fn load_row(db: &SqlitePool, show_id: i64, source: Source) -> Result<Option<EditRow>> {
    Ok(
        sqlx::query_as("SELECT * FROM recording_edits WHERE show_id = ? AND source = ?")
            .bind(show_id)
            .bind(source.as_str())
            .fetch_optional(db)
            .await?,
    )
}

/// The editor's view of the picked file. Also feeds the show detail.
#[derive(Debug, Serialize)]
pub struct EditView {
    /// Which file this is: `show` | `own`.
    file: Source,
    /// Name of the file, `None` when it does not exist.
    filename: Option<String>,
    source: Option<SourceView>,
    edit: Option<RecordingEdit>,
    /// `none` | `draft` | `rendering` | `done` | `failed`
    pub(crate) status: String,
    error: Option<String>,
    pub(crate) edited_url: Option<String>,
    pub(crate) rendered_at: Option<String>,
    #[serde(skip)]
    pub(crate) edited_seconds: Option<f64>,
}

#[derive(Debug, Serialize)]
struct SourceView {
    url: String,
    duration: Option<f64>,
}

pub async fn view(state: &Arc<AppState>, show: &models::Show) -> Result<EditView> {
    let (file, source) = picked_file(state, show).await?;
    let filename = source.as_ref().map(|s| s.filename.clone());
    let row = load_row(&state.db, show.id, file).await?;
    let edit = row
        .as_ref()
        .and_then(|r| serde_json::from_str(&r.edit_json).ok());
    let (Some(source), Some(row)) = (source.as_ref(), row.as_ref()) else {
        let source = match source {
            Some(s) => Some(source_view(state, &s).await?),
            None => None,
        };
        return Ok(EditView {
            file,
            filename,
            source,
            edit,
            status: "none".to_string(),
            error: None,
            edited_url: None,
            rendered_at: None,
            edited_seconds: None,
        });
    };
    let render = row.render_of(source);
    let edited_url = match render {
        Some(key) => Some(storage::get_presigned_url(state, key, 3600).await?),
        None => None,
    };
    // "done" means a render of the current recording exists.
    let status = if row.status == "done" && render.is_none() {
        "draft".to_string()
    } else {
        row.status.clone()
    };
    Ok(EditView {
        file,
        filename,
        source: Some(source_view(state, source).await?),
        edit,
        status,
        error: row.error_message.clone(),
        edited_url,
        rendered_at: render.and(row.rendered_at.clone()),
        edited_seconds: render.and(row.edited_seconds),
    })
}

async fn source_view(state: &Arc<AppState>, source: &RecordingSource) -> Result<SourceView> {
    Ok(SourceView {
        url: storage::get_presigned_url_from_bucket(state, &source.bucket, &source.key, 3600)
            .await?,
        duration: source.duration(),
    })
}

fn no_recording() -> AppError {
    AppError::NotFound("Show has no recording".to_string())
}

fn invalid(problems: Vec<String>) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({ "error": problems.join("; "), "problems": problems })),
    )
        .into_response()
}

// ============================================================================
// Handlers
// ============================================================================

/// `GET /api/shows/:id/recording/edit`
pub async fn api_get_recording_edit(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<Json<EditView>> {
    let (_user, show) = require_show_editor(&state, &headers, id).await?;
    Ok(Json(view(&state, &show).await?))
}

#[derive(Debug, Deserialize)]
pub struct SourceBody {
    source: Source,
}

/// `PUT /api/shows/:id/recording/source` — pick the file for SoundCloud.
pub async fn api_put_recording_source(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    Json(body): Json<SourceBody>,
) -> Result<Json<serde_json::Value>> {
    let (_user, show) = require_show_editor(&state, &headers, id).await?;
    if resolve_file(&state, &show, body.source).await?.is_none() {
        return Err(AppError::BadRequest(
            match body.source {
                Source::Own => "There is no own file.",
                Source::Show => "There is no show recording yet.",
            }
            .to_string(),
        ));
    }
    sqlx::query("UPDATE shows SET recording_source = ? WHERE id = ?")
        .bind(body.source.as_str())
        .bind(id)
        .execute(&state.db)
        .await?;
    Ok(Json(json!({ "source": body.source })))
}

/// `PUT /api/shows/:id/recording/edit` — save the edit as a draft.
pub async fn api_put_recording_edit(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response> {
    let (_user, show) = require_show_editor(&state, &headers, id).await?;
    let (picked, source) = picked_file(&state, &show).await?;
    let source = source.ok_or_else(no_recording)?;
    let edit: RecordingEdit = match serde_json::from_slice(&body) {
        Ok(edit) => edit,
        Err(e) => return Ok(invalid(vec![format!("Invalid edit JSON: {e}")])),
    };
    let problems = validate(&edit, source.duration());
    if !problems.is_empty() {
        return Ok(invalid(problems));
    }
    let edit_json = serde_json::to_string(&edit)
        .map_err(|e| AppError::Internal(format!("Failed to encode the edit: {e}")))?;
    let saved = sqlx::query(
        "INSERT INTO recording_edits (show_id, source, edit_json) VALUES (?, ?, ?) \
         ON CONFLICT(show_id, source) DO UPDATE SET edit_json = excluded.edit_json, \
             status = 'draft', error_message = NULL, updated_at = datetime('now') \
         WHERE recording_edits.status != 'rendering'",
    )
    .bind(id)
    .bind(picked.as_str())
    .bind(&edit_json)
    .execute(&state.db)
    .await?;
    if saved.rows_affected() == 0 {
        return Err(AppError::Conflict(
            "The edit is rendering; wait until it is done".to_string(),
        ));
    }
    Ok(Json(view(&state, &show).await?).into_response())
}

/// `POST /api/shows/:id/recording/edit/apply` — render the saved edit.
pub async fn api_apply_recording_edit(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<Response> {
    let (_user, show) = require_show_editor(&state, &headers, id).await?;
    let (picked, source) = picked_file(&state, &show).await?;
    let source = source.ok_or_else(no_recording)?;
    let row = load_row(&state.db, id, picked)
        .await?
        .ok_or_else(|| AppError::BadRequest("Save an edit first".to_string()))?;
    let edit: RecordingEdit = serde_json::from_str(&row.edit_json)
        .map_err(|e| AppError::BadRequest(format!("The saved edit is not valid: {e}")))?;
    let problems = validate(&edit, source.duration());
    if !problems.is_empty() {
        return Ok(invalid(problems));
    }
    let claimed = sqlx::query(
        "UPDATE recording_edits SET status = 'rendering', error_message = NULL, \
             updated_at = datetime('now') \
         WHERE show_id = ? AND source = ? AND status != 'rendering'",
    )
    .bind(id)
    .bind(picked.as_str())
    .execute(&state.db)
    .await?;
    if claimed.rows_affected() == 0 {
        return Err(AppError::Conflict(
            "The edit is already rendering".to_string(),
        ));
    }
    tokio::spawn(render_task(state.clone(), id, picked, edit, source));
    Ok((StatusCode::ACCEPTED, Json(json!({ "status": "rendering" }))).into_response())
}

/// `GET /api/shows/:id/recording/peaks` — the waveform, computed once.
pub async fn api_get_recording_peaks(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<Response> {
    let (_user, show) = require_show_editor(&state, &headers, id).await?;
    let source = picked_file(&state, &show)
        .await?
        .1
        .ok_or_else(no_recording)?;
    let key = peaks_key(&source.key);
    if let Ok((data, _)) = storage::download_file(&state, &key).await {
        return Ok(([(header::CONTENT_TYPE, "application/json")], data).into_response());
    }
    {
        let mut jobs = state.peaks_jobs.lock().unwrap();
        match jobs.get(&key) {
            // Report the failure once; the next request starts a new job.
            Some(Some(_)) => {
                let error = jobs.remove(&key).flatten().unwrap_or_default();
                return Err(AppError::Internal(error));
            }
            Some(None) => {}
            None => {
                jobs.insert(key.clone(), None);
                tokio::spawn(peaks_task(state.clone(), id, source, key));
            }
        }
    }
    Ok((
        StatusCode::ACCEPTED,
        Json(json!({ "status": "processing" })),
    )
        .into_response())
}

// ============================================================================
// Background jobs
// ============================================================================

/// A fresh temp dir for one job; the caller removes it.
async fn job_dir(kind: &str) -> Result<PathBuf> {
    let dir = std::env::temp_dir().join(format!("{kind}_{}", uuid::Uuid::new_v4()));
    tokio::fs::create_dir_all(&dir)
        .await
        .map_err(|e| AppError::Internal(format!("Failed to create {}: {e}", dir.display())))?;
    Ok(dir)
}

/// The source downloaded into `dir`, keeping its extension for ffmpeg.
async fn download_source(
    state: &Arc<AppState>,
    source: &RecordingSource,
    dir: &FsPath,
) -> Result<PathBuf> {
    let ext = FsPath::new(&source.key)
        .extension()
        .map(|e| e.to_string_lossy().into_owned())
        .unwrap_or_else(|| "mp3".to_string());
    let path = dir.join(format!("source.{ext}"));
    storage::download_to_file(state, &source.bucket, &source.key, &path).await?;
    Ok(path)
}

fn ffmpeg_error(stderr: &[u8]) -> String {
    let stderr = String::from_utf8_lossy(stderr);
    let last = stderr.lines().last().unwrap_or("unknown error");
    format!("ffmpeg failed: {last}")
}

async fn render_task(
    state: Arc<AppState>,
    show_id: i64,
    picked: Source,
    edit: RecordingEdit,
    source: RecordingSource,
) {
    let result = render(&state, show_id, picked, &edit, &source).await;
    let previous = match load_row(&state.db, show_id, picked).await {
        Ok(row) => row.and_then(|r| r.edited_key),
        Err(_) => None,
    };
    let saved = match &result {
        Ok(key) => {
            sqlx::query(
                "UPDATE recording_edits SET status = 'done', edited_key = ?, source_key = ?, \
                     edited_seconds = ?, rendered_at = datetime('now'), error_message = NULL, \
                     updated_at = datetime('now') \
                 WHERE show_id = ? AND source = ?",
            )
            .bind(key)
            .bind(&source.key)
            .bind(edit.end - edit.start)
            .bind(show_id)
            .bind(picked.as_str())
            .execute(&state.db)
            .await
        }
        Err(e) => {
            tracing::error!(show_id, error = %e, "Recording edit render failed");
            // The previous edited file stays the upload source.
            sqlx::query(
                "UPDATE recording_edits SET status = 'failed', error_message = ?, \
                     updated_at = datetime('now') \
                 WHERE show_id = ? AND source = ?",
            )
            .bind(e.to_string())
            .bind(show_id)
            .bind(picked.as_str())
            .execute(&state.db)
            .await
        }
    };
    if let Err(e) = saved {
        tracing::error!(show_id, error = %e, "Failed to store the recording edit result");
        return;
    }
    if let (Ok(key), Some(old)) = (&result, previous) {
        if &old != key {
            if let Err(e) = state
                .s3_client
                .delete_object()
                .bucket(&state.config.r2_bucket_name)
                .key(&old)
                .send()
                .await
            {
                tracing::warn!(show_id, key = old.as_str(), error = %e, "Failed to delete the previous edited recording");
            }
        }
    }
}

/// Render `edit` from `source` and upload it. Returns the new key.
async fn render(
    state: &Arc<AppState>,
    show_id: i64,
    picked: Source,
    edit: &RecordingEdit,
    source: &RecordingSource,
) -> Result<String> {
    let dir = job_dir("recording_edit").await?;
    let result = async {
        let input = download_source(state, source, &dir).await?;
        let output = dir.join("edited.mp3");
        let run = Command::new("ffmpeg")
            .args(["-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i"])
            .arg(&input)
            .args(["-vn", "-af"])
            .arg(render_filter(edit))
            .args(["-c:a", "libmp3lame", "-b:a", "320k"])
            .arg(&output)
            .stdin(Stdio::null())
            .kill_on_drop(true)
            .output();
        let out = tokio::time::timeout(FFMPEG_TIMEOUT, run)
            .await
            .map_err(|_| AppError::Internal("ffmpeg timed out".to_string()))?
            .map_err(|e| AppError::Internal(format!("Failed to run ffmpeg: {e}")))?;
        if !out.status.success() {
            return Err(AppError::Internal(ffmpeg_error(&out.stderr)));
        }
        // shortcut: the whole mp3 sits in RAM for the upload (~290 MB for 2 h at
        // 320k), like the finalize upload; stream the parts if memory gets tight.
        let data = tokio::fs::read(&output)
            .await
            .map_err(|e| AppError::Internal(format!("Failed to read the edited mp3: {e}")))?;
        let key = format!(
            "recordings/{show_id}/edited/{}-{}.mp3",
            picked.as_str(),
            chrono::Utc::now().format("%Y%m%d-%H%M%S")
        );
        storage::upload_multipart(
            &state.s3_client,
            &state.config.r2_bucket_name,
            &key,
            data,
            "audio/mpeg",
        )
        .await?;
        Ok(key)
    }
    .await;
    let _ = tokio::fs::remove_dir_all(&dir).await;
    result
}

async fn peaks_task(state: Arc<AppState>, show_id: i64, source: RecordingSource, key: String) {
    let result = async {
        let dir = job_dir("recording_peaks").await?;
        let peaks = async {
            let input = download_source(&state, &source, &dir).await?;
            compute_peaks(&input).await
        }
        .await;
        let _ = tokio::fs::remove_dir_all(&dir).await;
        let body = serde_json::to_vec(&peaks?)
            .map_err(|e| AppError::Internal(format!("Failed to encode peaks: {e}")))?;
        state
            .s3_client
            .put_object()
            .bucket(&state.config.r2_bucket_name)
            .key(&key)
            .body(aws_sdk_s3::primitives::ByteStream::from(body))
            .content_type("application/json")
            .send()
            .await
            .map_err(|e| AppError::Storage(format!("Failed to upload peaks: {e}")))?;
        Ok::<_, AppError>(())
    }
    .await;
    let mut jobs = state.peaks_jobs.lock().unwrap();
    match result {
        Ok(()) => {
            jobs.remove(&key);
        }
        Err(e) => {
            tracing::error!(show_id, error = %e, "Recording peaks failed");
            jobs.insert(key, Some(e.to_string()));
        }
    }
}

/// Waveform data: per bucket the largest absolute sample, 0..1.
#[derive(Debug, PartialEq, Serialize)]
pub struct Peaks {
    pub duration: f64,
    pub per_second: u32,
    pub peaks: Vec<f64>,
}

/// Builds [`Peaks`] from s16le mono PCM fed in chunks of any size.
#[derive(Default)]
struct PeaksBuilder {
    peaks: Vec<f64>,
    max: i32,
    in_bucket: usize,
    samples: u64,
    /// The first byte of a sample split between two chunks.
    carry: Option<u8>,
}

impl PeaksBuilder {
    fn feed(&mut self, mut data: &[u8]) {
        if let (Some(low), Some(&high)) = (self.carry, data.first()) {
            self.sample(i16::from_le_bytes([low, high]));
            self.carry = None;
            data = &data[1..];
        }
        let (pairs, rest) = data.as_chunks::<2>();
        for pair in pairs {
            self.sample(i16::from_le_bytes(*pair));
        }
        if let [low] = rest {
            self.carry = Some(*low);
        }
    }

    fn sample(&mut self, s: i16) {
        self.max = self.max.max(i32::from(s).abs());
        self.in_bucket += 1;
        self.samples += 1;
        if self.in_bucket == PEAK_BUCKET {
            self.push();
        }
    }

    fn push(&mut self) {
        let peak = self.max as f64 / 32768.0;
        self.peaks.push((peak * 100.0).round() / 100.0);
        self.max = 0;
        self.in_bucket = 0;
    }

    fn finish(mut self) -> Peaks {
        if self.in_bucket > 0 {
            self.push();
        }
        let seconds = self.samples as f64 / PEAK_RATE as f64;
        Peaks {
            duration: (seconds * 1000.0).round() / 1000.0,
            per_second: PEAKS_PER_SECOND,
            peaks: self.peaks,
        }
    }
}

/// Decode `path` to 8 kHz mono and build its peaks, reading ffmpeg's output as
/// a stream (a 2 h set is ~115 MB of PCM).
async fn compute_peaks(path: &FsPath) -> Result<Peaks> {
    let mut child = Command::new("ffmpeg")
        .args(["-hide_banner", "-nostdin", "-loglevel", "error", "-i"])
        .arg(path)
        .args(["-vn", "-ac", "1", "-ar"])
        .arg(PEAK_RATE.to_string())
        .args(["-f", "s16le", "-"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| AppError::Internal(format!("Failed to run ffmpeg: {e}")))?;
    let mut stdout = child.stdout.take().expect("piped stdout");
    let mut stderr = child.stderr.take().expect("piped stderr");
    let work = async {
        let mut builder = PeaksBuilder::default();
        let mut errors = Vec::new();
        // Drain stderr alongside stdout, or a full stderr pipe stalls ffmpeg.
        let read_pcm = async {
            let mut buf = vec![0u8; 64 * 1024];
            loop {
                let n = stdout.read(&mut buf).await?;
                if n == 0 {
                    return Ok::<_, std::io::Error>(());
                }
                builder.feed(&buf[..n]);
            }
        };
        let (pcm, err) = tokio::join!(read_pcm, stderr.read_to_end(&mut errors));
        pcm.and(err.map(|_| ()))
            .map_err(|e| AppError::Internal(format!("Failed to read ffmpeg output: {e}")))?;
        let status = child
            .wait()
            .await
            .map_err(|e| AppError::Internal(format!("ffmpeg did not finish: {e}")))?;
        if !status.success() {
            return Err(AppError::Internal(ffmpeg_error(&errors)));
        }
        Ok(builder.finish())
    };
    tokio::time::timeout(FFMPEG_TIMEOUT, work)
        .await
        .map_err(|_| AppError::Internal("ffmpeg timed out".to_string()))?
}

#[cfg(test)]
#[path = "recording_edit_tests.rs"]
mod tests;
