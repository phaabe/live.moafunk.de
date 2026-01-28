use aws_sdk_s3::config::Credentials;
use aws_sdk_s3::primitives::ByteStream;
use rand::seq::SliceRandom;
use rand::Rng;
use sqlx::sqlite::SqlitePoolOptions;
use sqlx::SqlitePool;
use std::env;
use uuid::Uuid;

const DEFAULT_DATABASE_URL: &str = "sqlite:./data/unheard.db?mode=rwc";

const ONE_BY_ONE_PNG: &[u8] = &[
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
    0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4,
    0x89, 0x00, 0x00, 0x00, 0x0A, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9C, 0x63, 0x00, 0x01, 0x00, 0x00,
    0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE,
    0x42, 0x60, 0x82,
];

const DUMMY_MP3: &[u8] = b"ID3\x03\x00\x00\x00\x00\x00\x0Fdummy-audio";

#[derive(Debug, Clone)]
struct Args {
    database_url: String,
    count: u32,
    status: String,
    upload: bool,
}

fn parse_args() -> Result<Args, String> {
    let mut database_url = DEFAULT_DATABASE_URL.to_string();
    let mut count: u32 = 10;
    let mut status = "unassigned".to_string();
    let mut upload = false;

    let mut it = env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--database-url" => {
                database_url = it
                    .next()
                    .ok_or_else(|| "--database-url requires a value".to_string())?;
            }
            "--count" => {
                let v = it
                    .next()
                    .ok_or_else(|| "--count requires a value".to_string())?;
                count = v
                    .parse::<u32>()
                    .map_err(|_| "--count must be an integer".to_string())?;
            }
            "--status" => {
                status = it
                    .next()
                    .ok_or_else(|| "--status requires a value".to_string())?;
            }
            "--upload" => upload = true,
            "-h" | "--help" => {
                return Err(help_text());
            }
            other => return Err(format!("Unknown argument: {}\n{}", other, help_text())),
        }
    }

    if !matches!(status.as_str(), "assigned" | "unassigned") {
        return Err("--status must be one of: assigned, unassigned".to_string());
    }

    Ok(Args {
        database_url,
        count,
        status,
        upload,
    })
}

fn help_text() -> String {
    [
        "Seeds the SQLite DB with dummy artists.",
        "",
        "Usage:",
        "  cargo run --bin seed_dummy_artists -- [--count N] [--database-url URL] [--status assigned|unassigned] [--upload]",
        "",
        "Notes:",
        "  - Always satisfies DB-required artist columns.",
        "  - With --upload, uploads placeholder pic/tracks to Cloudflare R2 and stores the object keys.",
        "",
        "Required env vars for --upload:",
        "  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY",
        "Optional:",
        "  R2_BUCKET_NAME (default: unheard-artists-dev)",
    ]
    .join("\n")
}

async fn run_migrations(pool: &SqlitePool) -> Result<(), sqlx::Error> {
    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS artists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            pronouns TEXT NOT NULL,

            pic_key TEXT,
            pic_cropped_key TEXT,
            pic_overlay_key TEXT,
            voice_message_key TEXT,
            no_voice_message INTEGER NOT NULL DEFAULT 0,

            track1_name TEXT NOT NULL,
            track1_key TEXT,
            track2_name TEXT NOT NULL,
            track2_key TEXT,

            instagram TEXT,
            soundcloud TEXT,
            bandcamp TEXT,
            spotify TEXT,
            other_social TEXT,

            upcoming_events TEXT,
            mentions TEXT,

            status TEXT NOT NULL DEFAULT 'unassigned',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        )
        "#,
    )
    .execute(pool)
    .await?;

    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS shows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            date TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'scheduled',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        )
        "#,
    )
    .execute(pool)
    .await?;

    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS artist_show_assignments (
            artist_id INTEGER NOT NULL,
            show_id INTEGER NOT NULL,
            PRIMARY KEY (artist_id, show_id),
            FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE,
            FOREIGN KEY (show_id) REFERENCES shows(id) ON DELETE CASCADE
        )
        "#,
    )
    .execute(pool)
    .await?;

    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL
        )
        "#,
    )
    .execute(pool)
    .await?;

    // Match backend enforcement: at most 4 artists per show.
    sqlx::query(
        r#"
        CREATE TRIGGER IF NOT EXISTS trg_max_artists_per_show
        BEFORE INSERT ON artist_show_assignments
        BEGIN
            SELECT CASE
                WHEN (SELECT COUNT(*) FROM artist_show_assignments WHERE show_id = NEW.show_id) >= 4
                THEN RAISE(ABORT, 'show has maximum number of artists (4)')
            END;
        END;
        "#,
    )
    .execute(pool)
    .await?;

    Ok(())
}

fn random_pronouns(rng: &mut impl rand::Rng) -> &'static str {
    const PRONOUNS: &[&str] = &["she/her", "he/him", "they/them", "she/they", "he/they"];
    PRONOUNS.choose(rng).copied().unwrap_or("they/them")
}

fn random_name(rng: &mut impl rand::Rng) -> String {
    const FIRST: &[&str] = &[
        "Alex", "Sam", "Jordan", "Taylor", "Robin", "Charlie", "Casey", "Morgan", "Avery", "Riley",
        "Jamie", "Noah", "Mila", "Lea", "Nina", "Theo", "Kai", "Mika", "Sasha",
    ];
    const LAST: &[&str] = &[
        "Nova", "Flux", "Echo", "Stone", "Vega", "Quartz", "Lumen", "Orchid", "Wolfe", "Violet",
        "Blaze", "Moss", "Tide", "Frost", "Skylark", "Night", "Drift",
    ];
    const PREFIX: &[&str] = &["DJ", "MC", "" /* none */, "" /* none */];

    let prefix = PREFIX.choose(rng).copied().unwrap_or("");
    let first = FIRST.choose(rng).copied().unwrap_or("Alex");
    let last = LAST.choose(rng).copied().unwrap_or("Nova");

    if prefix.is_empty() {
        format!("{} {}", first, last)
    } else {
        format!("{} {} {}", prefix, first, last)
    }
}

fn maybe_url(rng: &mut impl rand::Rng, base: &str, handle: &str) -> Option<String> {
    if rng.gen_bool(0.6) {
        Some(format!("{}/{}", base.trim_end_matches('/'), handle))
    } else {
        None
    }
}

fn build_handle(name: &str) -> String {
    let mut out = String::new();
    for ch in name.chars() {
        if ch.is_ascii_alphanumeric() {
            out.push(ch.to_ascii_lowercase());
        }
    }
    if out.is_empty() {
        "artist".to_string()
    } else {
        out
    }
}

async fn build_r2_client() -> Result<(aws_sdk_s3::Client, String), String> {
    let account_id =
        env::var("R2_ACCOUNT_ID").map_err(|_| "Missing env var R2_ACCOUNT_ID".to_string())?;
    let access_key_id =
        env::var("R2_ACCESS_KEY_ID").map_err(|_| "Missing env var R2_ACCESS_KEY_ID".to_string())?;
    let secret_access_key = env::var("R2_SECRET_ACCESS_KEY")
        .map_err(|_| "Missing env var R2_SECRET_ACCESS_KEY".to_string())?;

    let bucket = env::var("R2_BUCKET_NAME").unwrap_or_else(|_| "unheard-artists-dev".to_string());
    let endpoint_url = format!("https://{}.r2.cloudflarestorage.com", account_id);

    // R2 requires path-style addressing (not virtual-hosted style)
    let conf = aws_sdk_s3::Config::builder()
        .endpoint_url(endpoint_url)
        .credentials_provider(Credentials::new(
            access_key_id,
            secret_access_key,
            None,
            None,
            "r2",
        ))
        .region(aws_sdk_s3::config::Region::new("auto"))
        .force_path_style(true)
        .build();

    Ok((aws_sdk_s3::Client::from_conf(conf), bucket))
}

async fn upload_object(
    client: &aws_sdk_s3::Client,
    bucket: &str,
    key: &str,
    bytes: Vec<u8>,
    content_type: &str,
) -> Result<(), String> {
    client
        .put_object()
        .bucket(bucket)
        .key(key)
        .body(ByteStream::from(bytes))
        .content_type(content_type)
        .send()
        .await
        .map_err(|e| format!("Upload failed for {}: {}", key, e))?;
    Ok(())
}

fn r2_key(artist_id: i64, kind: &str, ext: &str) -> String {
    let id = Uuid::new_v4().to_string();
    format!("artists/{}/{}/{}.{}", artist_id, kind, &id[..8], ext)
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args = match parse_args() {
        Ok(a) => a,
        Err(msg) => {
            eprintln!("{}", msg);
            std::process::exit(2);
        }
    };

    // Ensure the default ./data folder exists so sqlite can create the DB file.
    if args.database_url.contains("./data/") {
        let _ = std::fs::create_dir_all("data");
    }

    let pool = SqlitePoolOptions::new()
        .max_connections(5)
        .connect(&args.database_url)
        .await?;

    run_migrations(&pool).await?;

    let (r2_client, r2_bucket) = if args.upload {
        let (c, b) = build_r2_client().await.map_err(anyhow::Error::msg)?;
        (Some(c), Some(b))
    } else {
        (None, None)
    };

    let mut rng = rand::thread_rng();

    for _ in 0..args.count {
        let name = random_name(&mut rng);
        let pronouns = random_pronouns(&mut rng);
        let handle = build_handle(&name);

        let track1_name = format!(
            "{}",
            ["Opening", "Groove", "Pulse", "Neon", "Afterhours"]
                .choose(&mut rng)
                .copied()
                .unwrap_or("Opening")
        );
        let track2_name = format!(
            "{}",
            ["Second Wind", "Bassline", "Drift", "Moonlight", "Finale"]
                .choose(&mut rng)
                .copied()
                .unwrap_or("Second Wind")
        );

        let instagram = maybe_url(&mut rng, "https://instagram.com", &handle);
        let soundcloud = maybe_url(&mut rng, "https://soundcloud.com", &handle);
        let bandcamp = maybe_url(&mut rng, "https://bandcamp.com", &handle);
        let spotify = if rng.gen_bool(0.3) {
            Some(format!(
                "https://open.spotify.com/artist/{}",
                Uuid::new_v4().simple()
            ))
        } else {
            None
        };
        let other_social = if rng.gen_bool(0.15) {
            Some(format!("https://example.com/{}", handle))
        } else {
            None
        };

        let upcoming_events = if rng.gen_bool(0.35) {
            Some("Next show: TBD".to_string())
        } else {
            None
        };

        let mentions = if rng.gen_bool(0.35) {
            Some("Loves analog synths.".to_string())
        } else {
            None
        };

        // Insert only the required + nice-to-have text fields.
        let result = sqlx::query(
            r#"
            INSERT INTO artists (
                name, pronouns, no_voice_message,
                track1_name, track2_name,
                instagram, soundcloud, bandcamp, spotify, other_social,
                upcoming_events, mentions,
                status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            "#,
        )
        .bind(&name)
        .bind(pronouns)
        .bind(1i64) // default: no voice message
        .bind(&track1_name)
        .bind(&track2_name)
        .bind(&instagram)
        .bind(&soundcloud)
        .bind(&bandcamp)
        .bind(&spotify)
        .bind(&other_social)
        .bind(&upcoming_events)
        .bind(&mentions)
        .bind(&args.status)
        .execute(&pool)
        .await?;

        let artist_id = result.last_insert_rowid();

        // Optional upload to Cloudflare R2 and store keys.
        if let (Some(client), Some(bucket)) = (&r2_client, &r2_bucket) {
            let pic_key = r2_key(artist_id, "pic", "png");
            let pic_cropped_key = r2_key(artist_id, "pic_cropped", "png");
            let pic_overlay_key = r2_key(artist_id, "pic_overlay", "png");
            let track1_key = r2_key(artist_id, "track1", "mp3");
            let track2_key = r2_key(artist_id, "track2", "mp3");

            upload_object(
                client,
                bucket,
                &pic_key,
                ONE_BY_ONE_PNG.to_vec(),
                "image/png",
            )
            .await
            .map_err(anyhow::Error::msg)?;
            upload_object(
                client,
                bucket,
                &pic_cropped_key,
                ONE_BY_ONE_PNG.to_vec(),
                "image/png",
            )
            .await
            .map_err(anyhow::Error::msg)?;
            upload_object(
                client,
                bucket,
                &pic_overlay_key,
                ONE_BY_ONE_PNG.to_vec(),
                "image/png",
            )
            .await
            .map_err(anyhow::Error::msg)?;
            upload_object(
                client,
                bucket,
                &track1_key,
                DUMMY_MP3.to_vec(),
                "audio/mpeg",
            )
            .await
            .map_err(anyhow::Error::msg)?;
            upload_object(
                client,
                bucket,
                &track2_key,
                DUMMY_MP3.to_vec(),
                "audio/mpeg",
            )
            .await
            .map_err(anyhow::Error::msg)?;

            sqlx::query(
                r#"
                UPDATE artists SET
                    pic_key = ?,
                    pic_cropped_key = ?,
                    pic_overlay_key = ?,
                    track1_key = ?,
                    track2_key = ?
                WHERE id = ?
                "#,
            )
            .bind(&pic_key)
            .bind(&pic_cropped_key)
            .bind(&pic_overlay_key)
            .bind(&track1_key)
            .bind(&track2_key)
            .bind(artist_id)
            .execute(&pool)
            .await?;
        }

        println!("Created artist {}: {}", artist_id, name);
    }

    Ok(())
}
