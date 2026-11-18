//! Show series: what the host flow calls a "Show". A series has a name, a
//! description, a cover, a URL slug and a rhythm. Its dated episodes are rows
//! in `shows` that point to it with `template_id`.
//!
//! Series live in `show_templates`, so the admin ShowWizard template picker
//! keeps working on the same rows. New code says "series" so it does not
//! clash with the `shows` table, which holds episodes.

use std::collections::{HashMap, HashSet};

use chrono::{DateTime, Datelike, Duration, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Utc};
use chrono_tz::Europe::Berlin;
use serde::Serialize;
use sqlx::{SqliteConnection, SqlitePool};

/// Name of the one-time data step in `data_migrations`.
const SERIES_MIGRATION: &str = "series_v1";

/// Slugs are cut to this many characters before a `-N` suffix is added.
const MAX_SLUG_LEN: usize = 60;

/// How often `insert_series` retries when another insert took the slug first.
const SLUG_INSERT_ATTEMPTS: usize = 5;

const WEEKDAY_NAMES: [&str; 7] = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
];

// ============================================================================
// Slugs
// ============================================================================

/// Lowercase ASCII slug made from a series name. It is never empty, never
/// only digits and never `new`, so it can't clash with `/shows/<id>` or
/// `/shows/new`.
pub fn slug_base(name: &str) -> String {
    let mut raw = String::with_capacity(name.len());
    for c in name.chars().flat_map(char::to_lowercase) {
        match c {
            'a'..='z' | '0'..='9' => raw.push(c),
            other => raw.push_str(ascii_for(other)),
        }
    }

    let mut slug = String::with_capacity(raw.len());
    for part in raw.split('-').filter(|part| !part.is_empty()) {
        if !slug.is_empty() {
            slug.push('-');
        }
        slug.push_str(part);
    }
    slug.truncate(MAX_SLUG_LEN);
    let mut slug = slug.trim_end_matches('-').to_string();

    if slug.is_empty() {
        slug = "show".to_string();
    }
    if slug == "new" || slug.bytes().all(|b| b.is_ascii_digit()) {
        slug = format!("show-{slug}");
    }
    slug
}

/// ASCII spelling of a lowercase non-ASCII letter; `-` for anything else.
fn ascii_for(c: char) -> &'static str {
    match c {
        'ä' | 'æ' => "ae",
        'ö' | 'œ' => "oe",
        'ü' => "ue",
        'ß' => "ss",
        'à' | 'á' | 'â' | 'ã' | 'å' | 'ā' => "a",
        'ç' | 'ć' | 'č' => "c",
        'è' | 'é' | 'ê' | 'ë' | 'ē' => "e",
        'ì' | 'í' | 'î' | 'ï' => "i",
        'ł' => "l",
        'ñ' | 'ń' => "n",
        'ò' | 'ó' | 'ô' | 'õ' | 'ø' => "o",
        'ś' | 'š' => "s",
        'ù' | 'ú' | 'û' => "u",
        'ý' | 'ÿ' => "y",
        'ź' | 'ż' | 'ž' => "z",
        _ => "-",
    }
}

/// The first free slug for `name`: the base, then `<base>-2`, `<base>-3`, …
async fn unique_slug(conn: &mut SqliteConnection, name: &str) -> Result<String, sqlx::Error> {
    let base = slug_base(name);
    // `base` only holds [a-z0-9-], so it has no LIKE wildcards.
    let taken: HashSet<String> =
        sqlx::query_scalar("SELECT slug FROM show_templates WHERE slug = ? OR slug LIKE ? || '-%'")
            .bind(&base)
            .bind(&base)
            .fetch_all(&mut *conn)
            .await?
            .into_iter()
            .collect();

    if !taken.contains(&base) {
        return Ok(base);
    }
    let mut n = 2;
    loop {
        let candidate = format!("{base}-{n}");
        if !taken.contains(&candidate) {
            return Ok(candidate);
        }
        n += 1;
    }
}

fn is_unique_violation(error: &sqlx::Error) -> bool {
    matches!(error, sqlx::Error::Database(db) if db.is_unique_violation())
}

/// Insert a series with a fresh slug. Returns `(id, slug)`. Retries with the
/// next free slug if a parallel insert took the same one.
pub async fn insert_series(
    pool: &SqlitePool,
    owner_user_id: i64,
    name: &str,
    description: Option<&str>,
    rhythm: Option<&Rhythm>,
) -> Result<(i64, String), sqlx::Error> {
    let mut conn = pool.acquire().await?;
    let mut attempt = 1;
    loop {
        let slug = unique_slug(&mut conn, name).await?;
        let inserted = sqlx::query(
            "INSERT INTO show_templates \
             (owner_user_id, name, description, slug, repeat_rule, weekday, start_time, end_time) \
             VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        )
        .bind(owner_user_id)
        .bind(name)
        .bind(description)
        .bind(&slug)
        .bind(rhythm.map(|r| r.repeat_rule.as_str()))
        .bind(rhythm.map(|r| r.weekday))
        .bind(rhythm.map(|r| r.start_time.as_str()))
        .bind(rhythm.map(|r| r.end_time.as_str()))
        .execute(&mut *conn)
        .await;
        match inserted {
            Ok(done) => return Ok((done.last_insert_rowid(), slug)),
            Err(e) if is_unique_violation(&e) && attempt < SLUG_INSERT_ATTEMPTS => attempt += 1,
            Err(e) => return Err(e),
        }
    }
}

// ============================================================================
// Rhythm
// ============================================================================

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum RepeatRule {
    Weekly,
    Biweekly,
}

impl RepeatRule {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "weekly" => Some(Self::Weekly),
            "biweekly" => Some(Self::Biweekly),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Weekly => "weekly",
            Self::Biweekly => "biweekly",
        }
    }
}

/// When a series airs. `weekday` is ISO 8601: 1 = Monday … 7 = Sunday.
/// Times are "HH:MM" in Europe/Berlin.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Rhythm {
    pub repeat_rule: RepeatRule,
    pub weekday: i64,
    pub start_time: String,
    pub end_time: String,
}

impl Rhythm {
    /// Rhythm from the four nullable DB columns. `None` unless all are valid.
    pub fn from_columns(
        repeat_rule: Option<&str>,
        weekday: Option<i64>,
        start_time: Option<&str>,
        end_time: Option<&str>,
    ) -> Option<Self> {
        let repeat_rule = RepeatRule::parse(repeat_rule?)?;
        let weekday = weekday.filter(|d| (1..=7).contains(d))?;
        let (start_time, end_time) = (start_time?, end_time?);
        parse_hhmm(start_time)?;
        parse_hhmm(end_time)?;
        Some(Self {
            repeat_rule,
            weekday,
            start_time: start_time.to_string(),
            end_time: end_time.to_string(),
        })
    }

    /// The line on My Shows, e.g. "Sundays · 12:00–14:00".
    pub fn line(&self) -> String {
        let day = WEEKDAY_NAMES[(self.weekday - 1) as usize];
        let time = format!("{}–{}", self.start_time, self.end_time);
        match self.repeat_rule {
            RepeatRule::Weekly => format!("{day}s · {time}"),
            RepeatRule::Biweekly => format!("Every other {day} · {time}"),
        }
    }
}

/// Strict "HH:MM" (zero-padded, so text order is time order).
pub fn parse_hhmm(value: &str) -> Option<NaiveTime> {
    if value.len() != 5 {
        return None;
    }
    NaiveTime::parse_from_str(value, "%H:%M").ok()
}

/// The most common (weekday, start, end) of a series' dates, repeating weekly.
/// A tie goes to the slot that aired most recently. Dates without a valid
/// start and end are ignored.
fn rhythm_from_dates(dates: &[(String, String, String)]) -> Option<Rhythm> {
    let mut slots: HashMap<(i64, &str, &str), (usize, NaiveDate)> = HashMap::new();
    for (date, start, end) in dates {
        let Ok(day) = NaiveDate::parse_from_str(date, "%Y-%m-%d") else {
            continue;
        };
        if parse_hhmm(start).is_none() || parse_hhmm(end).is_none() {
            continue;
        }
        let weekday = i64::from(day.weekday().number_from_monday());
        let slot = slots
            .entry((weekday, start.as_str(), end.as_str()))
            .or_insert((0, day));
        slot.0 += 1;
        slot.1 = slot.1.max(day);
    }

    slots
        .into_iter()
        .max_by(|(key_a, (count_a, last_a)), (key_b, (count_b, last_b))| {
            count_a
                .cmp(count_b)
                .then(last_a.cmp(last_b))
                // Same count and same last date: the smaller key wins, so the
                // result never depends on hash order.
                .then_with(|| key_b.cmp(key_a))
        })
        .map(|((weekday, start, end), _)| Rhythm {
            repeat_rule: RepeatRule::Weekly,
            weekday,
            start_time: start.to_string(),
            end_time: end.to_string(),
        })
}

/// How many weeks ahead Book Slot offers dates.
pub const SLOT_WEEKS: i64 = 12;

/// The dates a rhythm airs on, from `from` (inclusive) for `weeks` weeks.
/// "Every 2 weeks" counts from the week of `anchor`, the series' latest
/// booked episode. Before the first booking (no anchor) every week is offered.
pub fn rhythm_dates(
    rhythm: &Rhythm,
    anchor: Option<NaiveDate>,
    from: NaiveDate,
    weeks: i64,
) -> Vec<NaiveDate> {
    let until = from + Duration::weeks(weeks);
    let ahead = (rhythm.weekday - i64::from(from.weekday().number_from_monday())).rem_euclid(7);
    let mut day = from + Duration::days(ahead);
    let mut dates = Vec::new();
    while day < until {
        let in_phase = match (rhythm.repeat_rule, anchor) {
            (RepeatRule::Biweekly, Some(anchor)) => weeks_between(anchor, day).rem_euclid(2) == 0,
            _ => true,
        };
        if in_phase {
            dates.push(day);
        }
        day += Duration::weeks(1);
    }
    dates
}

/// Whole weeks between the Mondays of the weeks of `a` and `b`.
fn weeks_between(a: NaiveDate, b: NaiveDate) -> i64 {
    let monday = |d: NaiveDate| d - Duration::days(i64::from(d.weekday().num_days_from_monday()));
    (monday(b) - monday(a)).num_days() / 7
}

// ============================================================================
// Episode status
// ============================================================================

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EpisodeStatus {
    Upcoming,
    WrapupOpen,
    Finished,
}

impl EpisodeStatus {
    pub fn label(self) -> &'static str {
        match self {
            Self::Upcoming => "Upcoming",
            Self::WrapupOpen => "Finished · wrap-up open",
            Self::Finished => "Finished",
        }
    }
}

/// When an episode ends, in UTC. Times are Europe/Berlin. Without an end
/// time the series rhythm end is used; without both, the end of the date.
/// An end at or before the start is on the next day.
pub fn episode_end_utc(
    date: &str,
    start_time: Option<&str>,
    end_time: Option<&str>,
    rhythm_end: Option<&str>,
) -> Option<DateTime<Utc>> {
    let day = NaiveDate::parse_from_str(date, "%Y-%m-%d").ok()?;
    let end = end_time
        .and_then(parse_hhmm)
        .or_else(|| rhythm_end.and_then(parse_hhmm));
    let local = match end {
        Some(end) => {
            let mut local = day.and_time(end);
            if start_time
                .and_then(parse_hhmm)
                .is_some_and(|start| end <= start)
            {
                local += Duration::days(1);
            }
            local
        }
        None => day.succ_opt()?.and_time(NaiveTime::MIN),
    };
    berlin_to_utc(local)
}

/// An episode ends at its scheduled end, or earlier when the host ended it
/// with End Episode (`live_ended_at`, RFC 3339).
pub fn ended_by_host(
    end: Option<DateTime<Utc>>,
    live_ended_at: Option<&str>,
) -> Option<DateTime<Utc>> {
    let stopped = live_ended_at
        .and_then(|at| DateTime::parse_from_rfc3339(at).ok())
        .map(|at| at.with_timezone(&Utc));
    match (end, stopped) {
        (Some(end), Some(stopped)) => Some(end.min(stopped)),
        (end, stopped) => end.or(stopped),
    }
}

pub(crate) fn berlin_to_utc(local: NaiveDateTime) -> Option<DateTime<Utc>> {
    Berlin
        .from_local_datetime(&local)
        .earliest()
        .map(|dt| dt.with_timezone(&Utc))
}

/// Before the end: Upcoming. After the end: wrap-up open until it is
/// published, then Finished. An end that can't be computed counts as not
/// reached yet. The end time never cuts a show: while the episode is on air
/// it is not over, also past its end time or midnight.
pub fn episode_status(
    end: Option<DateTime<Utc>>,
    published: bool,
    on_air: bool,
    now: DateTime<Utc>,
) -> EpisodeStatus {
    match end {
        Some(end) if now >= end && !on_air => {
            if published {
                EpisodeStatus::Finished
            } else {
                EpisodeStatus::WrapupOpen
            }
        }
        _ => EpisodeStatus::Upcoming,
    }
}

/// "Live" or "Pre-recorded", from `shows.stream_mode`.
pub fn mode_of(stream_mode: Option<&str>) -> (&'static str, &'static str) {
    match stream_mode {
        Some("prerecorded") => ("prerecorded", "Pre-recorded"),
        _ => ("live", "Live"),
    }
}

// ============================================================================
// Migration
// ============================================================================

/// What the one-time data step did, for the log and for tests.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct MigrationReport {
    pub linked: Vec<i64>,
    /// Dates whose title matches more than one template of their owners.
    pub ambiguous: Vec<i64>,
    pub rhythms: Vec<i64>,
    pub without_rhythm: Vec<i64>,
    /// Upcoming host dates that stay unlinked (admins fix them by hand).
    pub upcoming_unlinked: Vec<i64>,
    pub marked_finished: Vec<i64>,
}

/// Slugs for every template, the slug index, and once per database the data
/// step: link dates to series, fill rhythms, close old wrap-ups. Returns
/// `None` when the data step already ran. Safe to run on every boot.
pub async fn migrate(pool: &SqlitePool) -> Result<Option<MigrationReport>, sqlx::Error> {
    sqlx::query(
        "CREATE TABLE IF NOT EXISTS data_migrations (\
             name TEXT PRIMARY KEY, \
             applied_at TEXT NOT NULL DEFAULT (datetime('now')))",
    )
    .execute(pool)
    .await?;
    sqlx::query(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_show_templates_slug ON show_templates(slug)",
    )
    .execute(pool)
    .await?;
    sqlx::query("CREATE INDEX IF NOT EXISTS idx_shows_template_id ON shows(template_id)")
        .execute(pool)
        .await?;

    let mut tx = pool.begin().await?;
    let slugs = backfill_slugs(&mut tx).await?;
    if slugs > 0 {
        tracing::info!(count = slugs, "series migration: gave templates a slug");
    }

    let done: Option<String> =
        sqlx::query_scalar("SELECT name FROM data_migrations WHERE name = ?")
            .bind(SERIES_MIGRATION)
            .fetch_optional(&mut *tx)
            .await?;
    if done.is_some() {
        tx.commit().await?;
        return Ok(None);
    }

    let report = migrate_data(&mut tx, Utc::now()).await?;
    sqlx::query("INSERT INTO data_migrations (name) VALUES (?)")
        .bind(SERIES_MIGRATION)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;

    log_report(&report);
    Ok(Some(report))
}

/// Oldest templates first, so they keep the plain slug.
async fn backfill_slugs(conn: &mut SqliteConnection) -> Result<usize, sqlx::Error> {
    let missing: Vec<(i64, String)> =
        sqlx::query_as("SELECT id, name FROM show_templates WHERE slug IS NULL ORDER BY id")
            .fetch_all(&mut *conn)
            .await?;
    for (id, name) in &missing {
        let slug = unique_slug(conn, name).await?;
        sqlx::query("UPDATE show_templates SET slug = ? WHERE id = ?")
            .bind(slug)
            .bind(id)
            .execute(&mut *conn)
            .await?;
    }
    Ok(missing.len())
}

async fn migrate_data(
    conn: &mut SqliteConnection,
    now: DateTime<Utc>,
) -> Result<MigrationReport, sqlx::Error> {
    let mut report = MigrationReport::default();

    // 1. Link a date to the one template of its host or creator whose name
    //    equals its title. UNHEARD dates are never linked.
    let matches: Vec<(i64, i64, i64)> = sqlx::query_as(
        "SELECT s.id, COUNT(t.id), MIN(t.id) \
         FROM shows s JOIN show_templates t \
           ON t.name = s.title \
          AND (t.owner_user_id = s.host_user_id OR t.owner_user_id = s.created_by) \
         WHERE s.template_id IS NULL AND s.show_type != 'unheard' \
         GROUP BY s.id ORDER BY s.id",
    )
    .fetch_all(&mut *conn)
    .await?;
    for (show_id, count, template_id) in matches {
        if count > 1 {
            report.ambiguous.push(show_id);
            continue;
        }
        sqlx::query("UPDATE shows SET template_id = ? WHERE id = ?")
            .bind(template_id)
            .bind(show_id)
            .execute(&mut *conn)
            .await?;
        report.linked.push(show_id);
    }

    // 2. Rhythm from the linked dates.
    let templates: Vec<i64> =
        sqlx::query_scalar("SELECT id FROM show_templates WHERE weekday IS NULL ORDER BY id")
            .fetch_all(&mut *conn)
            .await?;
    for template_id in templates {
        let dates: Vec<(String, String, String)> = sqlx::query_as(
            "SELECT date, start_time, end_time FROM shows \
             WHERE template_id = ? AND start_time IS NOT NULL AND end_time IS NOT NULL",
        )
        .bind(template_id)
        .fetch_all(&mut *conn)
        .await?;
        let Some(rhythm) = rhythm_from_dates(&dates) else {
            report.without_rhythm.push(template_id);
            continue;
        };
        sqlx::query(
            "UPDATE show_templates \
             SET repeat_rule = ?, weekday = ?, start_time = ?, end_time = ? WHERE id = ?",
        )
        .bind(rhythm.repeat_rule.as_str())
        .bind(rhythm.weekday)
        .bind(&rhythm.start_time)
        .bind(&rhythm.end_time)
        .bind(template_id)
        .execute(&mut *conn)
        .await?;
        report.rhythms.push(template_id);
    }

    // 3. Every date that already ended is Finished, not "wrap-up open".
    //    Upcoming host dates that did not link are reported.
    let dates: Vec<OpenDate> = sqlx::query_as(
        "SELECT s.id, s.date, s.start_time, s.end_time, t.end_time AS rhythm_end, \
                s.template_id, s.host_user_id, s.show_type \
         FROM shows s LEFT JOIN show_templates t ON t.id = s.template_id \
         WHERE s.wrapup_published_at IS NULL ORDER BY s.id",
    )
    .fetch_all(&mut *conn)
    .await?;
    let stamp = now.format("%Y-%m-%d %H:%M:%S").to_string();
    for d in dates {
        let end = episode_end_utc(
            &d.date,
            d.start_time.as_deref(),
            d.end_time.as_deref(),
            d.rhythm_end.as_deref(),
        );
        if end.is_some_and(|end| end <= now) {
            sqlx::query("UPDATE shows SET wrapup_published_at = ? WHERE id = ?")
                .bind(&stamp)
                .bind(d.id)
                .execute(&mut *conn)
                .await?;
            report.marked_finished.push(d.id);
        } else if d.template_id.is_none() && d.host_user_id.is_some() && d.show_type != "unheard" {
            report.upcoming_unlinked.push(d.id);
        }
    }

    Ok(report)
}

/// A date whose wrap-up is not published yet.
#[derive(sqlx::FromRow)]
struct OpenDate {
    id: i64,
    date: String,
    start_time: Option<String>,
    end_time: Option<String>,
    rhythm_end: Option<String>,
    template_id: Option<i64>,
    host_user_id: Option<i64>,
    show_type: String,
}

fn log_report(report: &MigrationReport) {
    tracing::info!(
        linked = report.linked.len(),
        rhythms = report.rhythms.len(),
        marked_finished = report.marked_finished.len(),
        "series migration: done"
    );
    if !report.ambiguous.is_empty() {
        tracing::warn!(
            show_ids = ?report.ambiguous,
            "series migration: dates not linked, their owner has more than one template with that name"
        );
    }
    if !report.without_rhythm.is_empty() {
        tracing::warn!(
            template_ids = ?report.without_rhythm,
            "series migration: series without a rhythm, set it by hand"
        );
    }
    if !report.upcoming_unlinked.is_empty() {
        tracing::warn!(
            show_ids = ?report.upcoming_unlinked,
            "series migration: upcoming host dates not linked to a series, link them by hand"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    async fn db() -> SqlitePool {
        let pool = SqlitePool::connect("sqlite::memory:").await.unwrap();
        crate::db::run_migrations(&pool).await.unwrap();
        pool
    }

    /// Make the next `migrate` behave as on a database from before series.
    async fn forget_migration(pool: &SqlitePool) {
        sqlx::query("DELETE FROM data_migrations WHERE name = ?")
            .bind(SERIES_MIGRATION)
            .execute(pool)
            .await
            .unwrap();
    }

    async fn user(pool: &SqlitePool, name: &str) -> i64 {
        sqlx::query("INSERT INTO users (username, password_hash, role) VALUES (?, 'x', 'host')")
            .bind(name)
            .execute(pool)
            .await
            .unwrap()
            .last_insert_rowid()
    }

    /// A template as old code created it: no slug, no rhythm.
    async fn legacy_template(pool: &SqlitePool, owner: i64, name: &str) -> i64 {
        sqlx::query("INSERT INTO show_templates (owner_user_id, name) VALUES (?, ?)")
            .bind(owner)
            .bind(name)
            .execute(pool)
            .await
            .unwrap()
            .last_insert_rowid()
    }

    struct Date<'a> {
        title: &'a str,
        date: &'a str,
        start: Option<&'a str>,
        end: Option<&'a str>,
        host: Option<i64>,
        created_by: Option<i64>,
        show_type: &'a str,
    }

    impl<'a> Date<'a> {
        fn new(title: &'a str, date: &'a str, host: i64) -> Self {
            Self {
                title,
                date,
                start: Some("12:00"),
                end: Some("14:00"),
                host: Some(host),
                created_by: Some(host),
                show_type: "external",
            }
        }
    }

    async fn insert_date(pool: &SqlitePool, d: Date<'_>) -> i64 {
        sqlx::query(
            "INSERT INTO shows (title, date, start_time, end_time, host_user_id, created_by, show_type) \
             VALUES (?, ?, ?, ?, ?, ?, ?)",
        )
        .bind(d.title)
        .bind(d.date)
        .bind(d.start)
        .bind(d.end)
        .bind(d.host)
        .bind(d.created_by)
        .bind(d.show_type)
        .execute(pool)
        .await
        .unwrap()
        .last_insert_rowid()
    }

    async fn template_of(pool: &SqlitePool, show_id: i64) -> Option<i64> {
        sqlx::query_scalar("SELECT template_id FROM shows WHERE id = ?")
            .bind(show_id)
            .fetch_one(pool)
            .await
            .unwrap()
    }

    async fn slug_of(pool: &SqlitePool, template_id: i64) -> String {
        sqlx::query_scalar("SELECT slug FROM show_templates WHERE id = ?")
            .bind(template_id)
            .fetch_one(pool)
            .await
            .unwrap()
    }

    #[test]
    fn slug_is_lowercase_ascii_and_never_a_route_word() {
        assert_eq!(slug_base("Brunchtime"), "brunchtime");
        assert_eq!(slug_base("  Late   Night Radio! "), "late-night-radio");
        assert_eq!(slug_base("Müllers Bühne"), "muellers-buehne");
        assert_eq!(slug_base("Straße & Café"), "strasse-cafe");
        assert_eq!(slug_base("2024"), "show-2024");
        assert_eq!(slug_base("New"), "show-new");
        assert_eq!(slug_base("New Wave"), "new-wave");
        assert_eq!(slug_base("!!!"), "show");
        assert_eq!(slug_base("日本"), "show");
        let long = slug_base(&"a".repeat(100));
        assert_eq!(long.len(), MAX_SLUG_LEN);
        assert!(slug_base(&format!("{} b", "a".repeat(59))).len() <= MAX_SLUG_LEN);
        assert!(!slug_base(&format!("{}-b", "a".repeat(59))).ends_with('-'));
    }

    #[tokio::test]
    async fn slug_collisions_get_a_number_and_the_db_rejects_duplicates() {
        let pool = db().await;
        let alice = user(&pool, "alice").await;
        let bob = user(&pool, "bob").await;
        let mut slugs = Vec::new();
        for owner in [alice, bob, alice] {
            let (_, slug) = insert_series(&pool, owner, "Brunchtime", None, None)
                .await
                .unwrap();
            slugs.push(slug);
        }
        assert_eq!(slugs, ["brunchtime", "brunchtime-2", "brunchtime-3"]);
        // A name that is itself "<base>-2" still gets a free slug.
        let (_, slug) = insert_series(&pool, bob, "Brunchtime 2", None, None)
            .await
            .unwrap();
        assert_eq!(slug, "brunchtime-2-2");

        let duplicate = sqlx::query(
            "INSERT INTO show_templates (owner_user_id, name, slug) VALUES (?, 'X', ?)",
        )
        .bind(alice)
        .bind("brunchtime")
        .execute(&pool)
        .await
        .unwrap_err();
        assert!(is_unique_violation(&duplicate));
    }

    #[test]
    fn rhythm_line_names_the_day_and_time() {
        let mut rhythm =
            Rhythm::from_columns(Some("weekly"), Some(7), Some("12:00"), Some("14:00"))
                .expect("valid rhythm");
        assert_eq!(rhythm.line(), "Sundays · 12:00–14:00");
        rhythm.repeat_rule = RepeatRule::Biweekly;
        rhythm.weekday = 5;
        assert_eq!(rhythm.line(), "Every other Friday · 12:00–14:00");

        assert!(Rhythm::from_columns(None, Some(7), Some("12:00"), Some("14:00")).is_none());
        assert!(
            Rhythm::from_columns(Some("monthly"), Some(7), Some("12:00"), Some("14:00")).is_none()
        );
        assert!(
            Rhythm::from_columns(Some("weekly"), Some(8), Some("12:00"), Some("14:00")).is_none()
        );
        assert!(
            Rhythm::from_columns(Some("weekly"), Some(7), Some("9:00"), Some("14:00")).is_none()
        );
        assert!(Rhythm::from_columns(Some("weekly"), Some(7), Some("12:00"), None).is_none());
    }

    fn utc(s: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(s).unwrap().with_timezone(&Utc)
    }

    #[test]
    fn episode_end_uses_own_end_then_rhythm_end_then_end_of_date() {
        // Clocks go back at 03:00 on 25 Oct 2026, so its afternoon is CET (UTC+1).
        let own = episode_end_utc("2026-10-25", Some("12:00"), Some("14:00"), Some("16:00"));
        assert_eq!(own, Some(utc("2026-10-25T13:00:00Z")));
        let rhythm = episode_end_utc("2026-10-25", Some("12:00"), None, Some("16:00"));
        assert_eq!(rhythm, Some(utc("2026-10-25T15:00:00Z")));
        let day = episode_end_utc("2026-07-01", Some("12:00"), None, None);
        assert_eq!(day, Some(utc("2026-07-01T22:00:00Z")));
        let overnight = episode_end_utc("2026-07-01", Some("23:00"), Some("01:00"), None);
        assert_eq!(overnight, Some(utc("2026-07-01T23:00:00Z")));
        assert_eq!(episode_end_utc("not a date", None, None, None), None);
    }

    #[test]
    fn end_episode_before_the_end_ends_the_episode_early() {
        let end = Some(utc("2026-10-25T13:00:00Z"));
        let early = "2026-10-25T12:50:00+00:00";
        assert_eq!(ended_by_host(end, Some(early)), Some(utc(early)));
        // Overtime: the scheduled end stays (the status uses "on air" for it).
        assert_eq!(ended_by_host(end, Some("2026-10-25T13:30:00Z")), end);
        assert_eq!(ended_by_host(end, None), end);
        assert_eq!(ended_by_host(end, Some("garbage")), end);
    }

    #[test]
    fn status_is_upcoming_then_wrapup_open_then_finished() {
        let end = Some(utc("2026-10-25T13:00:00Z"));
        let before = utc("2026-10-25T12:59:00Z");
        let after = utc("2026-10-25T13:00:00Z");
        assert_eq!(
            episode_status(end, false, false, before),
            EpisodeStatus::Upcoming
        );
        assert_eq!(
            episode_status(end, false, false, after),
            EpisodeStatus::WrapupOpen
        );
        assert_eq!(
            episode_status(end, true, false, after),
            EpisodeStatus::Finished
        );
        assert_eq!(
            episode_status(None, false, false, after),
            EpisodeStatus::Upcoming
        );
        // Still on air after the end time: the show goes on.
        assert_eq!(
            episode_status(end, false, true, after),
            EpisodeStatus::Upcoming
        );
        assert_eq!(EpisodeStatus::WrapupOpen.label(), "Finished · wrap-up open");
    }

    #[tokio::test]
    async fn migration_gives_old_templates_a_slug_oldest_first() {
        let pool = db().await;
        let alice = user(&pool, "alice").await;
        let first = legacy_template(&pool, alice, "Brunchtime").await;
        let second = legacy_template(&pool, alice, "Brunchtime").await;
        let digits = legacy_template(&pool, alice, "2024").await;
        forget_migration(&pool).await;

        migrate(&pool).await.unwrap();
        assert_eq!(slug_of(&pool, first).await, "brunchtime");
        assert_eq!(slug_of(&pool, second).await, "brunchtime-2");
        assert_eq!(slug_of(&pool, digits).await, "show-2024");
    }

    #[tokio::test]
    async fn migration_links_dates_by_exact_title_and_owner() {
        let pool = db().await;
        let alice = user(&pool, "alice").await;
        let bob = user(&pool, "bob").await;
        let admin = user(&pool, "admin").await;
        let carol = user(&pool, "carol").await;
        let alice_brunch = legacy_template(&pool, alice, "Brunchtime").await;
        let bob_brunch = legacy_template(&pool, bob, "Brunchtime").await;
        legacy_template(&pool, alice, "Twice").await;
        legacy_template(&pool, alice, "Twice").await;

        let hosted = insert_date(&pool, Date::new("Brunchtime", "2026-10-04", alice)).await;
        let created = insert_date(
            &pool,
            Date {
                host: None,
                ..Date::new("Brunchtime", "2026-10-11", alice)
            },
        )
        .await;
        let for_bob = insert_date(
            &pool,
            Date {
                created_by: Some(admin),
                ..Date::new("Brunchtime", "2026-10-11", bob)
            },
        )
        .await;
        let other_case = insert_date(&pool, Date::new("brunchtime", "2099-01-04", alice)).await;
        let twice = insert_date(&pool, Date::new("Twice", "2099-01-04", alice)).await;
        let unheard = insert_date(
            &pool,
            Date {
                show_type: "unheard",
                ..Date::new("Brunchtime", "2099-01-04", alice)
            },
        )
        .await;
        let no_template = insert_date(&pool, Date::new("Brunchtime", "2099-01-04", carol)).await;
        // Host and creator both own a "Brunchtime": no single match.
        let two_owners = insert_date(
            &pool,
            Date {
                created_by: Some(bob),
                ..Date::new("Brunchtime", "2099-01-03", alice)
            },
        )
        .await;
        forget_migration(&pool).await;

        let report = migrate(&pool).await.unwrap().expect("data step runs once");
        assert_eq!(template_of(&pool, hosted).await, Some(alice_brunch));
        assert_eq!(template_of(&pool, created).await, Some(alice_brunch));
        assert_eq!(template_of(&pool, for_bob).await, Some(bob_brunch));
        for unlinked in [other_case, twice, unheard, no_template, two_owners] {
            assert_eq!(template_of(&pool, unlinked).await, None, "show {unlinked}");
        }
        assert_eq!(report.linked, [hosted, created, for_bob]);
        assert_eq!(report.ambiguous, [twice, two_owners]);
        assert_eq!(
            report.upcoming_unlinked,
            [other_case, twice, no_template, two_owners],
            "UNHEARD dates are not host series dates"
        );
    }

    #[tokio::test]
    async fn migration_fills_rhythm_from_the_most_common_slot() {
        let pool = db().await;
        let alice = user(&pool, "alice").await;
        let brunch = legacy_template(&pool, alice, "Brunchtime").await;
        let empty = legacy_template(&pool, alice, "Empty").await;
        let no_end = legacy_template(&pool, alice, "No End").await;
        for date in ["2026-10-04", "2026-10-11", "2026-10-18"] {
            insert_date(&pool, Date::new("Brunchtime", date, alice)).await;
        }
        insert_date(
            &pool,
            Date {
                start: Some("20:00"),
                end: Some("22:00"),
                ..Date::new("Brunchtime", "2026-10-24", alice)
            },
        )
        .await;
        insert_date(
            &pool,
            Date {
                end: None,
                ..Date::new("No End", "2026-10-04", alice)
            },
        )
        .await;
        forget_migration(&pool).await;

        let report = migrate(&pool).await.unwrap().unwrap();
        let row: (Option<String>, Option<i64>, Option<String>, Option<String>) = sqlx::query_as(
            "SELECT repeat_rule, weekday, start_time, end_time FROM show_templates WHERE id = ?",
        )
        .bind(brunch)
        .fetch_one(&pool)
        .await
        .unwrap();
        let rhythm =
            Rhythm::from_columns(row.0.as_deref(), row.1, row.2.as_deref(), row.3.as_deref())
                .expect("rhythm filled");
        assert_eq!(rhythm.line(), "Sundays · 12:00–14:00");
        assert_eq!(report.rhythms, [brunch]);
        assert_eq!(report.without_rhythm, [empty, no_end]);
    }

    fn day(s: &str) -> NaiveDate {
        NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap()
    }

    #[test]
    fn rhythm_dates_follow_the_weekday_and_every_other_week() {
        let sunday =
            Rhythm::from_columns(Some("weekly"), Some(7), Some("12:00"), Some("14:00")).unwrap();
        // From Wednesday 2026-11-18: the next Sundays, three weeks long.
        let dates = rhythm_dates(&sunday, None, day("2026-11-18"), 3);
        assert_eq!(
            dates,
            [day("2026-11-22"), day("2026-11-29"), day("2026-12-06")]
        );
        // The start day itself counts when it is the rhythm's weekday.
        assert_eq!(
            rhythm_dates(&sunday, None, day("2026-11-22"), 1),
            [day("2026-11-22")]
        );

        let biweekly = Rhythm {
            repeat_rule: RepeatRule::Biweekly,
            ..sunday
        };
        // No booking yet: every week is offered.
        assert_eq!(rhythm_dates(&biweekly, None, day("2026-11-18"), 3).len(), 3);
        // Counted from the latest episode (Sunday 2026-11-15), in both directions.
        let from_anchor = rhythm_dates(&biweekly, Some(day("2026-11-15")), day("2026-11-18"), 4);
        assert_eq!(from_anchor, [day("2026-11-29"), day("2026-12-13")]);
        // An anchor on another weekday counts by its week.
        let saturday_anchor =
            rhythm_dates(&biweekly, Some(day("2026-12-12")), day("2026-11-18"), 4);
        assert_eq!(saturday_anchor, [day("2026-11-29"), day("2026-12-13")]);
    }

    #[test]
    fn rhythm_tie_goes_to_the_most_recent_slot() {
        let dates = [
            (
                "2026-10-04".to_string(),
                "12:00".to_string(),
                "14:00".to_string(),
            ),
            (
                "2026-10-24".to_string(),
                "20:00".to_string(),
                "22:00".to_string(),
            ),
        ];
        let rhythm = rhythm_from_dates(&dates).unwrap();
        assert_eq!(rhythm.line(), "Saturdays · 20:00–22:00");
        assert!(rhythm_from_dates(&[]).is_none());
    }

    #[tokio::test]
    async fn migration_closes_wrapups_of_dates_that_already_ended() {
        let pool = db().await;
        let alice = user(&pool, "alice").await;
        let brunch = legacy_template(&pool, alice, "Brunchtime").await;
        let past = insert_date(&pool, Date::new("Old Show", "2020-01-05", alice)).await;
        let past_no_end = insert_date(
            &pool,
            Date {
                end: None,
                ..Date::new("Brunchtime", "2020-01-05", alice)
            },
        )
        .await;
        let future = insert_date(&pool, Date::new("Brunchtime", "2099-01-04", alice)).await;
        forget_migration(&pool).await;

        let report = migrate(&pool).await.unwrap().unwrap();
        assert_eq!(report.marked_finished, [past, past_no_end]);
        assert_eq!(template_of(&pool, past_no_end).await, Some(brunch));

        // A date that ends after the migration keeps its wrap-up open.
        let later = insert_date(&pool, Date::new("Brunchtime", "2020-01-12", alice)).await;
        crate::db::run_migrations(&pool).await.unwrap();
        let published: Vec<(i64, Option<String>)> =
            sqlx::query_as("SELECT id, wrapup_published_at FROM shows ORDER BY id")
                .fetch_all(&pool)
                .await
                .unwrap();
        let published: HashMap<i64, bool> = published
            .into_iter()
            .map(|(id, at)| (id, at.is_some()))
            .collect();
        assert!(published[&past] && published[&past_no_end]);
        assert!(!published[&future] && !published[&later]);
    }

    #[tokio::test]
    async fn second_migration_run_changes_nothing() {
        let pool = db().await;
        let alice = user(&pool, "alice").await;
        legacy_template(&pool, alice, "Brunchtime").await;
        legacy_template(&pool, alice, "Empty").await;
        insert_date(&pool, Date::new("Brunchtime", "2026-10-04", alice)).await;
        insert_date(&pool, Date::new("Brunchtime", "2099-01-04", alice)).await;
        insert_date(&pool, Date::new("Nothing", "2099-01-04", alice)).await;
        forget_migration(&pool).await;
        assert!(migrate(&pool).await.unwrap().is_some());

        async fn snapshot(pool: &SqlitePool) -> (Vec<String>, Vec<String>) {
            let shows = sqlx::query_scalar(
                "SELECT id || '|' || IFNULL(template_id, '') || '|' || IFNULL(wrapup_published_at, '') \
                 FROM shows ORDER BY id",
            )
            .fetch_all(pool)
            .await
            .unwrap();
            let templates = sqlx::query_scalar(
                "SELECT id || '|' || slug || '|' || IFNULL(repeat_rule, '') || '|' || \
                        IFNULL(weekday, '') || '|' || IFNULL(start_time, '') || '|' || IFNULL(end_time, '') \
                 FROM show_templates ORDER BY id",
            )
            .fetch_all(pool)
            .await
            .unwrap();
            (shows, templates)
        }
        let before = snapshot(&pool).await;
        assert_eq!(migrate(&pool).await.unwrap(), None, "no second data step");
        crate::db::run_migrations(&pool).await.unwrap();
        assert_eq!(snapshot(&pool).await, before);
    }

    #[tokio::test]
    async fn deleting_a_template_unlinks_its_dates() {
        let pool = db().await;
        let alice = user(&pool, "alice").await;
        let (series, _) = insert_series(&pool, alice, "Brunchtime", None, None)
            .await
            .unwrap();
        let date = insert_date(&pool, Date::new("Brunchtime", "2099-01-04", alice)).await;
        sqlx::query("UPDATE shows SET template_id = ? WHERE id = ?")
            .bind(series)
            .bind(date)
            .execute(&pool)
            .await
            .unwrap();

        sqlx::query("DELETE FROM show_templates WHERE id = ?")
            .bind(series)
            .execute(&pool)
            .await
            .unwrap();
        assert_eq!(template_of(&pool, date).await, None);
    }
}
