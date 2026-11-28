//! Dashboard chalkboard (#1049): short notes that every host and admin can
//! read and write. Stored here, not in Telegram.

use std::sync::Arc;

use axum::{
    extract::State,
    http::{HeaderMap, StatusCode},
    response::IntoResponse,
    Json,
};
use serde::{Deserialize, Serialize};

use super::api::require_show_creator;
use crate::{AppError, AppState, Result};

/// Longest note, in characters.
const MAX_CHARS: usize = 500;
/// Notes the board shows: the latest ones.
const SHOWN: i64 = 50;
/// Notes live one week. Older ones are deleted when the board loads.
const PRUNE: &str = "DELETE FROM chalkboard_notes WHERE created_at < datetime('now', '-7 days')";

const SELECT: &str = "SELECT n.id, u.username AS author, n.text, \
     CAST(strftime('%s', n.created_at) AS INTEGER) AS ts \
     FROM chalkboard_notes n JOIN users u ON u.id = n.user_id";

#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct ChalkNote {
    id: i64,
    author: String,
    text: String,
    /// Seconds since the epoch.
    ts: i64,
}

#[derive(Deserialize)]
pub struct NewNote {
    text: String,
}

/// GET /api/chalkboard — the latest notes of the last week, oldest first.
pub async fn api_list_chalkboard(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_show_creator(&state, &headers).await?;
    sqlx::query(PRUNE).execute(&state.db).await?;
    let mut notes: Vec<ChalkNote> = sqlx::query_as(&format!("{SELECT} ORDER BY n.id DESC LIMIT ?"))
        .bind(SHOWN)
        .fetch_all(&state.db)
        .await?;
    notes.reverse();
    Ok(Json(serde_json::json!({ "notes": notes })))
}

/// POST /api/chalkboard — write a note as the signed-in host or admin.
pub async fn api_add_chalkboard_note(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(body): Json<NewNote>,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;
    let text = body.text.trim();
    if text.is_empty() {
        return Err(AppError::Validation("The note is empty.".to_string()));
    }
    if text.chars().count() > MAX_CHARS {
        return Err(AppError::Validation(format!(
            "A note has at most {MAX_CHARS} characters."
        )));
    }
    let id = sqlx::query("INSERT INTO chalkboard_notes (user_id, text) VALUES (?, ?)")
        .bind(user.id)
        .bind(text)
        .execute(&state.db)
        .await?
        .last_insert_rowid();
    let note: ChalkNote = sqlx::query_as(&format!("{SELECT} WHERE n.id = ?"))
        .bind(id)
        .fetch_one(&state.db)
        .await?;
    Ok((StatusCode::CREATED, Json(note)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::broadcast_tests::{add_user, headers, state};
    use axum::body::to_bytes;
    use serde_json::Value;

    async fn add(state: &Arc<AppState>, user: i64, text: &str) -> Result<Value> {
        let response = api_add_chalkboard_note(
            State(state.clone()),
            headers(state, user).await,
            Json(NewNote {
                text: text.to_string(),
            }),
        )
        .await?
        .into_response();
        assert_eq!(response.status(), StatusCode::CREATED);
        Ok(
            serde_json::from_slice(&to_bytes(response.into_body(), 1 << 20).await.unwrap())
                .unwrap(),
        )
    }

    async fn list(state: &Arc<AppState>, user: i64) -> Result<Vec<Value>> {
        let response = api_list_chalkboard(State(state.clone()), headers(state, user).await)
            .await?
            .into_response();
        let body: Value =
            serde_json::from_slice(&to_bytes(response.into_body(), 1 << 20).await.unwrap())
                .unwrap();
        Ok(body["notes"].as_array().unwrap().clone())
    }

    #[tokio::test]
    async fn hosts_and_admins_share_one_board() {
        let (state, _temp) = state().await;
        let lena = add_user(&state, "lena", "host").await;
        let admin = add_user(&state, "boss", "admin").await;

        let note = add(&state, lena, "  Mic 2 is broken, use mic 1.  ")
            .await
            .unwrap();
        assert_eq!(note["author"], "lena");
        assert_eq!(note["text"], "Mic 2 is broken, use mic 1.");
        assert!(note["ts"].as_i64().unwrap() > 1_700_000_000);
        add(&state, admin, "Fixed.").await.unwrap();

        let texts: Vec<_> = list(&state, lena)
            .await
            .unwrap()
            .iter()
            .map(|n| format!("{}: {}", n["author"], n["text"]))
            .collect();
        assert_eq!(
            texts,
            [
                r#""lena": "Mic 2 is broken, use mic 1.""#,
                r#""boss": "Fixed.""#
            ]
        );
    }

    #[tokio::test]
    async fn refuses_empty_and_long_notes_and_guests() {
        let (state, _temp) = state().await;
        let lena = add_user(&state, "lena", "host").await;
        let guest = add_user(&state, "guest", "guest").await;

        assert!(matches!(
            add(&state, lena, "   ").await,
            Err(AppError::Validation(_))
        ));
        assert!(add(&state, lena, &"ü".repeat(MAX_CHARS)).await.is_ok());
        assert!(matches!(
            add(&state, lena, &"x".repeat(MAX_CHARS + 1)).await,
            Err(AppError::Validation(_))
        ));
        assert!(matches!(
            add(&state, guest, "hi").await,
            Err(AppError::Forbidden(_))
        ));
        assert!(matches!(
            list(&state, guest).await,
            Err(AppError::Forbidden(_))
        ));
        assert!(matches!(
            api_list_chalkboard(State(state.clone()), HeaderMap::new()).await,
            Err(AppError::Unauthorized(_))
        ));
    }

    #[tokio::test]
    async fn deletes_notes_older_than_one_week() {
        let (state, _temp) = state().await;
        let lena = add_user(&state, "lena", "host").await;
        for (text, age) in [("old", "-8 days"), ("recent", "-6 days"), ("new", "0 days")] {
            let id = add(&state, lena, text).await.unwrap()["id"]
                .as_i64()
                .unwrap();
            sqlx::query("UPDATE chalkboard_notes SET created_at = datetime('now', ?) WHERE id = ?")
                .bind(age)
                .bind(id)
                .execute(&state.db)
                .await
                .unwrap();
        }

        let texts: Vec<_> = list(&state, lena)
            .await
            .unwrap()
            .iter()
            .map(|n| n["text"].as_str().unwrap().to_string())
            .collect();
        assert_eq!(texts, ["recent", "new"]);
        let left: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM chalkboard_notes")
            .fetch_one(&state.db)
            .await
            .unwrap();
        assert_eq!(left, 2);
    }

    #[tokio::test]
    async fn shows_the_latest_notes_oldest_first() {
        let (state, _temp) = state().await;
        let lena = add_user(&state, "lena", "host").await;
        for i in 0..(SHOWN + 3) {
            add(&state, lena, &format!("note {i}")).await.unwrap();
        }
        let notes = list(&state, lena).await.unwrap();
        assert_eq!(notes.len(), SHOWN as usize);
        assert_eq!(notes[0]["text"], "note 3");
        assert_eq!(
            notes[notes.len() - 1]["text"],
            format!("note {}", SHOWN + 2)
        );
    }
}
