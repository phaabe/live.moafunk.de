use std::sync::Arc;

use chrono::{DateTime, NaiveDate, NaiveTime, TimeZone, Utc};
use chrono_tz::Europe::Berlin;

use crate::{models, telegram_notify, AppState};

/// How long after a live show's scheduled end we wait before alerting that no
/// recording was produced — avoids false alarms for a show that just ended or a
/// finalize still in flight.
const RECORDING_ALERT_GRACE_MINS: i64 = 15;

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

/// Parse an "HH:MM" string into a `NaiveTime`.
fn parse_hhmm(s: &str) -> Option<NaiveTime> {
    NaiveTime::parse_from_str(s, "%H:%M").ok()
}

/// Compute a show's scheduled end as a UTC instant, interpreting `date`+`end_time`
/// in Europe/Berlin. Handles overnight shows (end ≤ start → next calendar day).
/// Returns `None` if the date/time can't be parsed or the local time is invalid
/// (DST gap).
fn show_end_utc(date: &str, start_time: Option<&str>, end_time: &str) -> Option<DateTime<Utc>> {
    let day = NaiveDate::parse_from_str(date, "%Y-%m-%d").ok()?;
    let end = parse_hhmm(end_time)?;
    let mut end_dt = day.and_time(end);
    if let Some(start) = start_time.and_then(parse_hhmm) {
        if end <= start {
            end_dt += chrono::Duration::days(1);
        }
    }
    Berlin
        .from_local_datetime(&end_dt)
        .single()
        .map(|dt| dt.with_timezone(&Utc))
}

/// True if the show's scheduled end is at least `grace_mins` in the past relative
/// to `now` — i.e. it's been long enough that a missing recording is a real miss.
/// Operates on primitives so it's pure and unit-testable.
fn recording_overdue(
    date: &str,
    start_time: Option<&str>,
    end_time: Option<&str>,
    now: DateTime<Utc>,
    grace_mins: i64,
) -> bool {
    match show_end_utc(date, start_time, end_time.unwrap_or("")) {
        Some(end) => now >= end + chrono::Duration::minutes(grace_mins),
        None => false,
    }
}

/// Dead-man's-switch: alert (exactly once) when a live show that should have been
/// recorded produced no usable recording.
///
/// A show qualifies if it is live-mode, ended at least [`RECORDING_ALERT_GRACE_MINS`]
/// ago, has no successful `recording_versions` row (statuses raw/finalizing/finalized
/// — a short/corrupt recording is marked `failed` by the upload verifier and so
/// counts as missing), and has not already been alerted. Scoped to the last 2 days
/// so enabling the feature never back-alerts the whole archive.
pub async fn check_missing_recordings(state: Arc<AppState>) {
    let now = Utc::now();

    let shows: Vec<models::Show> = match sqlx::query_as(
        "SELECT * FROM shows s \
         WHERE s.stream_mode = 'live' \
           AND s.recording_alert_sent_at IS NULL \
           AND s.end_time IS NOT NULL \
           AND s.date >= date('now', '-2 days') \
           AND NOT EXISTS ( \
             SELECT 1 FROM recording_versions rv \
             WHERE rv.show_id = s.id \
               AND rv.status IN ('raw', 'finalizing', 'finalized') \
           )",
    )
    .fetch_all(&state.db)
    .await
    {
        Ok(rows) => rows,
        Err(e) => {
            tracing::error!("Dead-man's-switch: failed to query shows: {e}");
            return;
        }
    };

    for show in &shows {
        if !recording_overdue(
            &show.date,
            show.start_time.as_deref(),
            show.end_time.as_deref(),
            now,
            RECORDING_ALERT_GRACE_MINS,
        ) {
            continue; // still live, just ended, or unparseable schedule
        }

        tracing::warn!(
            "Dead-man's-switch: show {} ('{}') ended with no recording — alerting",
            show.id,
            show.title
        );

        telegram_notify::notify(
            &state,
            &format!(
                "⚠️ No recording for show \"{}\" (#{}) on {}{}. The live archive may be missing — check the recorder.",
                show.title,
                show.id,
                show.date,
                show.end_time.as_deref().map(|t| format!(" ending {t}")).unwrap_or_default(),
            ),
        )
        .await;

        // Mark alerted regardless of Telegram delivery so we never spam on retry.
        if let Err(e) =
            sqlx::query("UPDATE shows SET recording_alert_sent_at = datetime('now') WHERE id = ?")
                .bind(show.id)
                .execute(&state.db)
                .await
        {
            tracing::error!(
                "Dead-man's-switch: failed to mark show {} as alerted: {e}",
                show.id
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn show_end_utc_handles_evening_show() {
        // 20:00 Berlin (CEST, summer) → 18:00 UTC.
        let end = show_end_utc("2026-06-01", Some("18:00"), "20:00").unwrap();
        assert_eq!(end.to_rfc3339(), "2026-06-01T18:00:00+00:00");
    }

    #[test]
    fn show_end_utc_handles_overnight() {
        // 23:00 → 01:00 crosses midnight, so end is the next day 01:00 Berlin.
        let end = show_end_utc("2026-06-01", Some("23:00"), "01:00").unwrap();
        // 01:00 CEST on 2026-06-02 → 23:00 UTC on 2026-06-01.
        assert_eq!(end.to_rfc3339(), "2026-06-01T23:00:00+00:00");
    }

    #[test]
    fn show_end_utc_rejects_garbage() {
        assert!(show_end_utc("not-a-date", Some("18:00"), "20:00").is_none());
        assert!(show_end_utc("2026-06-01", None, "nope").is_none());
    }

    #[test]
    fn recording_overdue_respects_grace() {
        let end = show_end_utc("2026-06-01", Some("18:00"), "20:00").unwrap();
        // 10 min after end: within 15-min grace → not overdue.
        assert!(!recording_overdue(
            "2026-06-01",
            Some("18:00"),
            Some("20:00"),
            end + chrono::Duration::minutes(10),
            15
        ));
        // 20 min after end: past grace → overdue.
        assert!(recording_overdue(
            "2026-06-01",
            Some("18:00"),
            Some("20:00"),
            end + chrono::Duration::minutes(20),
            15
        ));
        // Unparseable schedule is never overdue (don't alert blindly).
        assert!(!recording_overdue(
            "bad",
            None,
            Some("20:00"),
            end + chrono::Duration::minutes(60),
            15
        ));
    }
}
