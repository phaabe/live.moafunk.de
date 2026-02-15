use std::sync::Arc;

use chrono::{NaiveDate, Utc};
use chrono_tz::Europe::Berlin;

use crate::{models, telegram_notify, AppState};

/// Check if any artist Instagram previews need to be sent today.
///
/// Logic:
/// - For each show in the last 30 days: compute `days_since = today - show.date`
/// - If `days_since` falls within 1..=artist_count, pick the artist at index `days_since - 1`
/// - If that artist hasn't been sent yet (telegram_artist_preview_sent_at IS NULL)
///   and has a caption (instagram_caption IS NOT NULL), send the preview
pub async fn check_artist_preview_schedule(state: Arc<AppState>) {
    let today = Utc::now().with_timezone(&Berlin).date_naive();

    // Shows from the past 30 days
    let shows: Vec<models::Show> = match sqlx::query_as(
        "SELECT * FROM shows WHERE date >= date('now', '-30 days') AND date < date('now') ORDER BY date ASC",
    )
    .fetch_all(&state.db)
    .await
    {
        Ok(rows) => rows,
        Err(e) => {
            tracing::error!("Scheduler: failed to query shows: {e}");
            return;
        }
    };

    for show in &shows {
        let show_date = match NaiveDate::parse_from_str(&show.date, "%Y-%m-%d") {
            Ok(d) => d,
            Err(e) => {
                tracing::warn!(
                    "Scheduler: cannot parse show date '{}' for show {}: {e}",
                    show.date,
                    show.id
                );
                continue;
            }
        };

        let days_since = (today - show_date).num_days();
        if days_since < 1 {
            continue;
        }

        // Fetch assigned artists in sort order
        let artists: Vec<models::Artist> = match sqlx::query_as(
            "SELECT a.* FROM artists a \
             INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id \
             WHERE asa.show_id = ? ORDER BY asa.sort_order, a.name COLLATE NOCASE",
        )
        .bind(show.id)
        .fetch_all(&state.db)
        .await
        {
            Ok(rows) => rows,
            Err(e) => {
                tracing::error!(
                    "Scheduler: failed to query artists for show {}: {e}",
                    show.id
                );
                continue;
            }
        };

        let artist_count = artists.len() as i64;
        if days_since > artist_count {
            continue; // Past the last artist for this show
        }

        let idx = (days_since - 1) as usize;
        let artist = &artists[idx];

        // Guard: already sent
        if artist.telegram_artist_preview_sent_at.is_some() {
            tracing::debug!(
                "Scheduler: artist {} (show {}) already sent, skipping",
                artist.id,
                show.id
            );
            continue;
        }

        // Guard: no caption
        if artist.instagram_caption.is_none() {
            tracing::warn!(
                "Scheduler: artist {} '{}' has no instagram_caption, skipping preview for show {}",
                artist.id,
                artist.name,
                show.id
            );
            continue;
        }

        tracing::info!(
            "Scheduler: sending preview for artist {} '{}' (day {} of show {} '{}')",
            artist.id,
            artist.name,
            days_since,
            show.id,
            show.title
        );

        match telegram_notify::send_artist_instagram_preview(&state, artist).await {
            Ok(()) => {
                tracing::info!(
                    "Scheduler: preview sent for artist {} '{}'",
                    artist.id,
                    artist.name
                );
            }
            Err(e) => {
                tracing::error!(
                    "Scheduler: failed to send preview for artist {} '{}': {e}",
                    artist.id,
                    artist.name
                );
            }
        }
    }
}
