//! Instagram announcements of an episode: settings and posting limits.
//! An episode has at most one announcement per slot (pre, onair, post).
//! `pre` and `post` are feed posts, `onair` is a story.

use chrono::{DateTime, Datelike, NaiveDate, SecondsFormat, Timelike, Utc};
use chrono_tz::Europe::Berlin;
use serde::{Deserialize, Serialize};
use sqlx::SqlitePool;

use crate::db;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AnnouncementSlot {
    Pre,
    OnAir,
    Post,
}

impl AnnouncementSlot {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "pre" => Some(Self::Pre),
            "onair" => Some(Self::OnAir),
            "post" => Some(Self::Post),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Pre => "pre",
            Self::OnAir => "onair",
            Self::Post => "post",
        }
    }

    /// Feed post (pre, post) or story (onair).
    pub fn is_feed(self) -> bool {
        self != Self::OnAir
    }
}

/// States that count against the limits.
/// cancelled, rejected, failed and draft do not count.
pub const ACTIVE_STATES: &str =
    "('pending_approval', 'scheduled', 'armed', 'waiting_soundcloud', 'sent')";

#[derive(Debug, Clone, Serialize, sqlx::FromRow)]
pub struct Announcement {
    pub id: i64,
    pub show_id: i64,
    pub slot: String,
    pub channel: String,
    pub state: String,
    pub anchor_offset_minutes: Option<i64>,
    pub scheduled_at: Option<String>,
    pub headline: Option<String>,
    pub caption: Option<String>,
    pub image_source: String,
    pub image_key: Option<String>,
    pub layout: String,
    pub collaborator: bool,
    pub created_by: Option<i64>,
    pub created_at: String,
    pub updated_at: String,
    pub sent_at: Option<String>,
    pub permalink: Option<String>,
    pub media_id: Option<String>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ApprovalMode {
    /// The first two posts of a host need an admin's approval.
    FirstTwo,
    Always,
    Never,
}

impl ApprovalMode {
    fn parse(value: &str) -> Option<Self> {
        match value {
            "first_two" => Some(Self::FirstTwo),
            "always" => Some(Self::Always),
            "never" => Some(Self::Never),
            _ => None,
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::FirstTwo => "first_two",
            Self::Always => "always",
            Self::Never => "never",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AnnouncementSettings {
    pub limit_per_episode: u32,
    pub feed_per_day: u32,
    pub feed_per_week: u32,
    pub stories_per_day: u32,
    /// Days before the episode the pre post goes out by default.
    pub pre_days_before: u32,
    /// Feed post times, "HH:MM" Europe/Berlin.
    pub slots: Vec<String>,
    pub approval_mode: ApprovalMode,
    pub paused: bool,
}

impl Default for AnnouncementSettings {
    fn default() -> Self {
        Self {
            limit_per_episode: 3,
            feed_per_day: 1,
            feed_per_week: 5,
            stories_per_day: 6,
            pre_days_before: 2,
            slots: vec!["12:00".to_string(), "18:00".to_string()],
            approval_mode: ApprovalMode::FirstTwo,
            paused: false,
        }
    }
}

/// "12:00, 18:00" as sorted "HH:MM" times, or `None` when one is not a time.
pub fn parse_slots(value: &str) -> Option<Vec<String>> {
    let mut slots = value
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|s| crate::series::parse_hhmm(s).map(|t| t.format("%H:%M").to_string()))
        .collect::<Option<Vec<_>>>()?;
    slots.sort();
    slots.dedup();
    Some(slots)
}

/// Settings from `app_settings`; a missing or bad value uses its default.
pub async fn get_settings(db: &SqlitePool) -> AnnouncementSettings {
    let mut s = AnnouncementSettings::default();
    let get = |key: &'static str| async move { db::get_setting(db, key).await.ok().flatten() };
    let num = |v: Option<String>, d: u32| v.and_then(|v| v.trim().parse().ok()).unwrap_or(d);
    s.limit_per_episode = num(get("announce_limit_per_episode").await, s.limit_per_episode);
    s.feed_per_day = num(get("announce_feed_per_day").await, s.feed_per_day);
    s.feed_per_week = num(get("announce_feed_per_week").await, s.feed_per_week);
    s.stories_per_day = num(get("announce_stories_per_day").await, s.stories_per_day);
    s.pre_days_before = num(get("announce_pre_days_before").await, s.pre_days_before);
    if let Some(slots) = get("announce_slots").await.and_then(|v| parse_slots(&v)) {
        s.slots = slots;
    }
    if let Some(mode) = get("announce_approval_mode")
        .await
        .and_then(|v| ApprovalMode::parse(v.trim()))
    {
        s.approval_mode = mode;
    }
    if let Some(paused) = get("announce_paused").await {
        s.paused = paused.trim() == "true";
    }
    s
}

pub async fn save_settings(db: &SqlitePool, s: &AnnouncementSettings) -> Result<(), sqlx::Error> {
    let pairs = [
        (
            "announce_limit_per_episode",
            s.limit_per_episode.to_string(),
        ),
        ("announce_feed_per_day", s.feed_per_day.to_string()),
        ("announce_feed_per_week", s.feed_per_week.to_string()),
        ("announce_stories_per_day", s.stories_per_day.to_string()),
        ("announce_pre_days_before", s.pre_days_before.to_string()),
        ("announce_slots", s.slots.join(",")),
        (
            "announce_approval_mode",
            s.approval_mode.as_str().to_string(),
        ),
        ("announce_paused", s.paused.to_string()),
    ];
    for (key, value) in pairs {
        db::set_setting(db, key, &value).await?;
    }
    Ok(())
}

/// Arms the on-air story of a newly booked episode at its start. Keeps an
/// existing row; does nothing when the start can't be computed.
pub async fn arm_onair(
    conn: &mut sqlx::SqliteConnection,
    show_id: i64,
    date: &str,
    start_time: &str,
    created_by: i64,
) -> Result<(), sqlx::Error> {
    let start = NaiveDate::parse_from_str(date, "%Y-%m-%d")
        .ok()
        .zip(crate::series::parse_hhmm(start_time))
        .and_then(|(day, time)| crate::series::berlin_to_utc(day.and_time(time)));
    let Some(start) = start else {
        return Ok(());
    };
    sqlx::query(
        "INSERT OR IGNORE INTO announcements (show_id, slot, channel, state, \
           anchor_offset_minutes, scheduled_at, created_by) \
         VALUES (?, 'onair', 'instagram', 'armed', 0, ?, ?)",
    )
    .bind(show_id)
    .bind(to_db_time(start))
    .bind(created_by)
    .execute(conn)
    .await?;
    Ok(())
}

/// One announcement with a send time.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Booked {
    pub show_id: i64,
    pub slot: AnnouncementSlot,
    pub scheduled_at: DateTime<Utc>,
}

/// The stored form of a send time.
pub fn to_db_time(t: DateTime<Utc>) -> String {
    t.to_rfc3339_opts(SecondsFormat::Secs, true)
}

fn berlin_day(t: DateTime<Utc>) -> NaiveDate {
    t.with_timezone(&Berlin).date_naive()
}

/// Whether `t` is one of the configured feed times (Berlin).
pub fn is_slot_time(settings: &AnnouncementSettings, t: DateTime<Utc>) -> bool {
    let local = t.with_timezone(&Berlin);
    local.second() == 0
        && local.nanosecond() == 0
        && settings.slots.contains(&local.format("%H:%M").to_string())
}

/// Refuses `candidate` when it breaks a limit. `others` are the active
/// announcements (pending_approval, scheduled, armed, sent); a row for the
/// candidate's own show and slot is ignored, as the candidate replaces it.
pub fn check_limits(
    settings: &AnnouncementSettings,
    candidate: &Booked,
    others: &[Booked],
) -> Result<(), String> {
    let others: Vec<&Booked> = others
        .iter()
        .filter(|o| !(o.show_id == candidate.show_id && o.slot == candidate.slot))
        .collect();

    let same_episode = others
        .iter()
        .filter(|o| o.show_id == candidate.show_id)
        .count();
    if same_episode as u32 >= settings.limit_per_episode {
        return Err(format!(
            "This episode already has {} announcements (limit {})",
            same_episode, settings.limit_per_episode
        ));
    }

    let day = berlin_day(candidate.scheduled_at);
    if candidate.slot.is_feed() {
        if !is_slot_time(settings, candidate.scheduled_at) {
            return Err(format!(
                "Feed posts can only go out at {} (Berlin time)",
                settings.slots.join(", ")
            ));
        }
        let feed: Vec<&&Booked> = others.iter().filter(|o| o.slot.is_feed()).collect();
        if feed
            .iter()
            .any(|o| o.scheduled_at == candidate.scheduled_at)
        {
            return Err("This time slot is already taken".to_string());
        }
        let per_day = feed
            .iter()
            .filter(|o| berlin_day(o.scheduled_at) == day)
            .count();
        if per_day as u32 >= settings.feed_per_day {
            return Err(format!(
                "The feed already has {} posts on {} (limit {} per day)",
                per_day, day, settings.feed_per_day
            ));
        }
        let week = day.iso_week();
        let per_week = feed
            .iter()
            .filter(|o| berlin_day(o.scheduled_at).iso_week() == week)
            .count();
        if per_week as u32 >= settings.feed_per_week {
            return Err(format!(
                "The feed already has {} posts in week {} (limit {} per week)",
                per_week,
                week.week(),
                settings.feed_per_week
            ));
        }
    } else {
        let stories = others
            .iter()
            .filter(|o| !o.slot.is_feed() && berlin_day(o.scheduled_at) == day)
            .count();
        if stories as u32 >= settings.stories_per_day {
            return Err(format!(
                "There are already {} stories on {} (limit {} per day)",
                stories, day, settings.stories_per_day
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    /// Berlin local time as UTC.
    fn at(y: i32, m: u32, d: u32, h: u32, min: u32) -> DateTime<Utc> {
        Berlin
            .with_ymd_and_hms(y, m, d, h, min, 0)
            .unwrap()
            .with_timezone(&Utc)
    }

    fn booked(show_id: i64, slot: AnnouncementSlot, t: DateTime<Utc>) -> Booked {
        Booked {
            show_id,
            slot,
            scheduled_at: t,
        }
    }

    use AnnouncementSlot::*;

    #[test]
    fn feed_time_must_be_a_slot_and_free() {
        let s = AnnouncementSettings::default();
        // 2030-06-03 is a Monday.
        let c = booked(1, Pre, at(2030, 6, 3, 18, 0));
        assert!(check_limits(&s, &c, &[]).is_ok());
        let off = booked(1, Pre, at(2030, 6, 3, 17, 0));
        assert!(check_limits(&s, &off, &[])
            .unwrap_err()
            .contains("only go out at"));
        let clash = check_limits(&s, &c, &[booked(2, Post, at(2030, 6, 3, 18, 0))]);
        assert_eq!(clash.unwrap_err(), "This time slot is already taken");
    }

    #[test]
    fn feed_per_day_and_week() {
        let s = AnnouncementSettings::default();
        let noon = booked(1, Pre, at(2030, 6, 3, 12, 0));
        let err = check_limits(&s, &noon, &[booked(2, Post, at(2030, 6, 3, 18, 0))]);
        assert!(err.unwrap_err().contains("per day"));

        // Five posts Mon-Fri fill the week; Sunday of that week is refused,
        // the Monday after is fine.
        let week: Vec<Booked> = (3..8)
            .map(|d| booked(10 + d as i64, Post, at(2030, 6, d, 18, 0)))
            .collect();
        let sunday = booked(1, Pre, at(2030, 6, 9, 18, 0));
        assert!(check_limits(&s, &sunday, &week)
            .unwrap_err()
            .contains("per week"));
        let monday = booked(1, Pre, at(2030, 6, 10, 18, 0));
        assert!(check_limits(&s, &monday, &week).is_ok());
    }

    #[test]
    fn stories_per_day_and_per_episode() {
        let s = AnnouncementSettings::default();
        let story = booked(1, OnAir, at(2030, 6, 3, 20, 0));
        let six: Vec<Booked> = (0..6)
            .map(|i| booked(10 + i, OnAir, at(2030, 6, 3, 8 + i as u32, 0)))
            .collect();
        assert!(check_limits(&s, &story, &six)
            .unwrap_err()
            .contains("stories"));
        assert!(check_limits(&s, &story, &six[..5]).is_ok());

        let s = AnnouncementSettings {
            limit_per_episode: 1,
            ..AnnouncementSettings::default()
        };
        let mine = [booked(1, Pre, at(2030, 6, 1, 18, 0))];
        assert!(check_limits(&s, &story, &mine)
            .unwrap_err()
            .contains("limit 1"));
        // The candidate's own row is replaced, not counted.
        let again = booked(1, Pre, at(2030, 6, 1, 18, 0));
        assert!(check_limits(&s, &again, &mine).is_ok());
    }

    #[test]
    fn slots_parse_and_reject_bad_times() {
        assert_eq!(
            parse_slots("18:00, 12:00"),
            Some(vec!["12:00".to_string(), "18:00".to_string()])
        );
        assert_eq!(parse_slots("12:00,noon"), None);
    }
}
