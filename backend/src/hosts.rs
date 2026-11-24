//! Who hosts a Show (series) and its episodes: the owner, the co-hosts the
//! owner added to the Show, and the co-hosts added to one episode only.
//!
//! - A Show co-host has the owner's rights on the Show and all its episodes,
//!   except changing who its co-hosts are.
//! - An episode co-host has the owner's rights on that one episode, except
//!   cancelling it and changing who hosts it.

use std::collections::HashMap;

use serde::Serialize;
use sqlx::SqlitePool;

use crate::models::{Show, User};

pub async fn migrate(pool: &SqlitePool) -> Result<(), sqlx::Error> {
    sqlx::query(
        "CREATE TABLE IF NOT EXISTS series_hosts (\
             series_id INTEGER NOT NULL REFERENCES show_templates(id) ON DELETE CASCADE, \
             user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, \
             added_by INTEGER REFERENCES users(id) ON DELETE SET NULL, \
             added_at TEXT NOT NULL DEFAULT (datetime('now')), \
             PRIMARY KEY (series_id, user_id))",
    )
    .execute(pool)
    .await?;
    sqlx::query(
        "CREATE TABLE IF NOT EXISTS episode_hosts (\
             show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE, \
             user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, \
             added_by INTEGER REFERENCES users(id) ON DELETE SET NULL, \
             added_at TEXT NOT NULL DEFAULT (datetime('now')), \
             PRIMARY KEY (show_id, user_id))",
    )
    .execute(pool)
    .await?;
    sqlx::query("CREATE INDEX IF NOT EXISTS idx_series_hosts_user ON series_hosts(user_id)")
        .execute(pool)
        .await?;
    sqlx::query("CREATE INDEX IF NOT EXISTS idx_episode_hosts_user ON episode_hosts(user_id)")
        .execute(pool)
        .await?;
    Ok(())
}

/// SQL condition: user `?2` hosts episode `s` (assigned host, the Show's owner
/// or co-host, or an episode co-host). Bind the user id as the 2nd parameter.
pub const HOSTS_EPISODE_SQL: &str = "(s.host_user_id = ?2 \
     OR EXISTS (SELECT 1 FROM show_templates ht WHERE ht.id = s.template_id \
                AND (ht.owner_user_id = ?2 OR EXISTS (SELECT 1 FROM series_hosts sh \
                     WHERE sh.series_id = ht.id AND sh.user_id = ?2))) \
     OR EXISTS (SELECT 1 FROM episode_hosts eh WHERE eh.show_id = s.id AND eh.user_id = ?2))";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum SeriesRole {
    Owner,
    Cohost,
}

/// The user's role on a Show, or `None` when they do not host it.
pub async fn series_role(
    db: &SqlitePool,
    series_id: i64,
    owner_user_id: i64,
    user_id: i64,
) -> Result<Option<SeriesRole>, sqlx::Error> {
    if owner_user_id == user_id {
        return Ok(Some(SeriesRole::Owner));
    }
    let cohost: Option<i64> =
        sqlx::query_scalar("SELECT 1 FROM series_hosts WHERE series_id = ? AND user_id = ?")
            .bind(series_id)
            .bind(user_id)
            .fetch_optional(db)
            .await?;
    Ok(cohost.map(|_| SeriesRole::Cohost))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EpisodeAccess {
    /// Everything, also cancelling the episode and changing who hosts it:
    /// admins, the Show's owner and co-hosts, and the assigned host. A show
    /// without a Show (the old wizard) also lets its creator manage it.
    Manage,
    /// An episode co-host: everything on this episode except managing it.
    Host,
}

/// What `user` may do on `show`, or `None` when they do not host it.
pub async fn episode_access(
    db: &SqlitePool,
    user: &User,
    show: &Show,
) -> Result<Option<EpisodeAccess>, sqlx::Error> {
    if user.role_enum().can_access_admin() || show.host_user_id == Some(user.id) {
        return Ok(Some(EpisodeAccess::Manage));
    }
    let series: Option<(i64, i64)> = sqlx::query_as(
        "SELECT t.id, t.owner_user_id FROM shows s JOIN show_templates t ON t.id = s.template_id \
         WHERE s.id = ?",
    )
    .bind(show.id)
    .fetch_optional(db)
    .await?;
    match series {
        Some((series_id, owner)) => {
            if series_role(db, series_id, owner, user.id).await?.is_some() {
                return Ok(Some(EpisodeAccess::Manage));
            }
        }
        // A booked episode's creator gets no rights of their own: a removed
        // co-host must not keep the episodes they booked.
        None if show.created_by == Some(user.id) => return Ok(Some(EpisodeAccess::Manage)),
        None => {}
    }
    let cohost: Option<i64> =
        sqlx::query_scalar("SELECT 1 FROM episode_hosts WHERE show_id = ? AND user_id = ?")
            .bind(show.id)
            .bind(user.id)
            .fetch_optional(db)
            .await?;
    Ok(cohost.map(|_| EpisodeAccess::Host))
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Host {
    pub user_id: i64,
    pub username: String,
    /// "owner" | "cohost" (of the Show) | "episode" (co-host of one episode)
    pub role: &'static str,
}

/// The hosts of a Show: its owner first, then its co-hosts in the order they
/// were added.
pub async fn series_hosts(db: &SqlitePool, series_id: i64) -> Result<Vec<Host>, sqlx::Error> {
    let owner: Option<(i64, String)> = sqlx::query_as(
        "SELECT u.id, u.username FROM show_templates t JOIN users u ON u.id = t.owner_user_id \
         WHERE t.id = ?",
    )
    .bind(series_id)
    .fetch_optional(db)
    .await?;
    let cohosts: Vec<(i64, String)> = sqlx::query_as(
        "SELECT u.id, u.username FROM series_hosts sh JOIN users u ON u.id = sh.user_id \
         WHERE sh.series_id = ? ORDER BY sh.added_at, sh.rowid",
    )
    .bind(series_id)
    .fetch_all(db)
    .await?;
    let owner = owner.into_iter().map(|row| (row, "owner"));
    let cohosts = cohosts.into_iter().map(|row| (row, "cohost"));
    Ok(owner
        .chain(cohosts)
        .map(|((user_id, username), role)| Host {
            user_id,
            username,
            role,
        })
        .collect())
}

/// The hosts of an episode: its Show's hosts, then its own co-hosts. An
/// episode without a Show has only its assigned host.
pub async fn episode_hosts(db: &SqlitePool, show: &Show) -> Result<Vec<Host>, sqlx::Error> {
    let series_id: Option<i64> = sqlx::query_scalar("SELECT template_id FROM shows WHERE id = ?")
        .bind(show.id)
        .fetch_one(db)
        .await?;
    let mut hosts = match series_id {
        Some(series_id) => series_hosts(db, series_id).await?,
        None => match show.host_user_id {
            Some(id) => {
                sqlx::query_as::<_, (i64, String)>("SELECT id, username FROM users WHERE id = ?")
                    .bind(id)
                    .fetch_optional(db)
                    .await?
                    .map(|(user_id, username)| Host {
                        user_id,
                        username,
                        role: "owner",
                    })
                    .into_iter()
                    .collect()
            }
            None => Vec::new(),
        },
    };
    let own: Vec<(i64, String)> = sqlx::query_as(
        "SELECT u.id, u.username FROM episode_hosts eh JOIN users u ON u.id = eh.user_id \
         WHERE eh.show_id = ? ORDER BY eh.added_at, eh.rowid",
    )
    .bind(show.id)
    .fetch_all(db)
    .await?;
    for (user_id, username) in own {
        if hosts.iter().all(|h| h.user_id != user_id) {
            hosts.push(Host {
                user_id,
                username,
                role: "episode",
            });
        }
    }
    Ok(hosts)
}

/// Every Show's co-hosts and every episode's co-hosts by name, in the order
/// they were added: for the lists of all hosts' episodes on the Dashboard.
#[derive(Default)]
pub struct HostNames {
    series: HashMap<i64, Vec<String>>,
    episodes: HashMap<i64, Vec<String>>,
}

impl HostNames {
    pub async fn load(db: &SqlitePool) -> Result<Self, sqlx::Error> {
        let mut names = Self::default();
        let series: Vec<(i64, String)> = sqlx::query_as(
            "SELECT sh.series_id, u.username FROM series_hosts sh \
             JOIN users u ON u.id = sh.user_id ORDER BY sh.added_at, sh.rowid",
        )
        .fetch_all(db)
        .await?;
        for (id, name) in series {
            names.series.entry(id).or_default().push(name);
        }
        let episodes: Vec<(i64, String)> = sqlx::query_as(
            "SELECT eh.show_id, u.username FROM episode_hosts eh \
             JOIN users u ON u.id = eh.user_id ORDER BY eh.added_at, eh.rowid",
        )
        .fetch_all(db)
        .await?;
        for (id, name) in episodes {
            names.episodes.entry(id).or_default().push(name);
        }
        Ok(names)
    }

    /// The owner, the Show's co-hosts, then the episode's co-hosts, each once.
    pub fn of_episode(&self, owner: &str, series_id: i64, show_id: i64) -> Vec<String> {
        let mut hosts = vec![owner.to_string()];
        let more = self.series.get(&series_id).into_iter().flatten();
        for name in more.chain(self.episodes.get(&show_id).into_iter().flatten()) {
            if !hosts.contains(name) {
                hosts.push(name.clone());
            }
        }
        hosts
    }

    /// The owner, then the Show's co-hosts.
    pub fn of_series(&self, owner: &str, series_id: i64) -> Vec<String> {
        let mut hosts = vec![owner.to_string()];
        hosts.extend(self.series.get(&series_id).into_iter().flatten().cloned());
        hosts
    }
}

/// Whether the user co-hosts at least one episode of this Show.
pub async fn cohosts_an_episode_of(
    db: &SqlitePool,
    series_id: i64,
    user_id: i64,
) -> Result<bool, sqlx::Error> {
    let found: Option<i64> = sqlx::query_scalar(
        "SELECT 1 FROM episode_hosts eh JOIN shows s ON s.id = eh.show_id \
         WHERE s.template_id = ? AND eh.user_id = ? LIMIT 1",
    )
    .bind(series_id)
    .bind(user_id)
    .fetch_optional(db)
    .await?;
    Ok(found.is_some())
}

/// The user a host may add as a co-host: a host account (no guests, no admins).
pub async fn find_host_account(db: &SqlitePool, user_id: i64) -> Result<Option<User>, sqlx::Error> {
    sqlx::query_as("SELECT * FROM users WHERE id = ? AND role = 'host'")
        .bind(user_id)
        .fetch_optional(db)
        .await
}
