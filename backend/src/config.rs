use serde::de::Error as _; // brings `envy::Error::custom` (serde::de::Error) into scope
use serde::Deserialize;

#[derive(Clone, Debug, Deserialize)]
pub struct Config {
    #[serde(default = "default_host")]
    pub host: String,
    #[serde(default = "default_port")]
    pub port: u16,

    pub secret_key: String,

    // Superadmin credentials (seeded on first run if no users exist)
    #[serde(default = "default_superadmin_username")]
    pub superadmin_username: String,
    pub superadmin_password_hash: String,

    #[serde(default = "default_database_url")]
    pub database_url: String,

    #[serde(default = "default_max_file_size")]
    pub max_file_size_mb: u64,

    #[serde(default = "default_max_upload_size")]
    pub max_upload_size_mb: u64,

    // R2 settings
    pub r2_account_id: String,
    pub r2_access_key_id: String,
    pub r2_secret_access_key: String,
    #[serde(default = "default_bucket_name")]
    pub r2_bucket_name: String,

    // Computed R2 endpoint
    #[serde(skip)]
    pub r2_endpoint: String,

    // Live producer output target.
    // "rtmp" (default) pushes FLV to NodeMediaServer; "icecast" pushes MP3 to an
    // Icecast mount (the Hetzner stream stack). See `producer_target()`.
    #[serde(default = "default_stream_output")]
    pub stream_output: String,

    // RTMP streaming settings (used when stream_output = "rtmp")
    #[serde(default = "default_rtmp_url")]
    pub rtmp_url: String,
    #[serde(default = "default_rtmp_stream_key")]
    pub rtmp_stream_key: String,

    // Icecast streaming settings (used when stream_output = "icecast").
    // Full source URL incl. credentials + mount, e.g.
    // `icecast://source:hackme@127.0.0.1:8010/live.mp3`. Required when
    // stream_output = "icecast" (validated at load).
    pub icecast_url: Option<String>,
    #[serde(default = "default_icecast_bitrate")]
    pub icecast_bitrate: String,
    #[serde(default = "default_icecast_sample_rate")]
    pub icecast_sample_rate: u32,
    // Icecast status endpoint for the listener/quality poller (#177), e.g.
    // `http://127.0.0.1:8010/status-json.xsl`. If unset, it's derived from
    // `icecast_url` (host:port → http://host:port/status-json.xsl); if neither
    // is set, the poller stays disabled.
    pub icecast_status_url: Option<String>,
    #[serde(default = "default_icecast_metrics_poll_secs")]
    pub icecast_metrics_poll_secs: u64,

    // Optional assets used for ZIP-time image stamping
    // If not set, the code will fall back to local paths under ./data.
    pub overlay_font_path: Option<String>,
    pub artist_logo_dir: Option<String>,
    pub default_logo_path: Option<String>,

    // FFmpeg MP3 conversion settings
    #[serde(default = "default_ffmpeg_bitrate")]
    pub ffmpeg_mp3_bitrate: String,
    #[serde(default = "default_ffmpeg_sample_rate")]
    pub ffmpeg_mp3_sample_rate: u32,

    // GitHub Actions backup trigger (optional)
    // If set, a GitHub repository_dispatch event will be triggered after artist submission
    pub github_dispatch_token: Option<String>,
    #[serde(default = "default_github_repo")]
    pub github_repo: String,

    // Instagram API settings (optional - for automatic posting to Instagram)
    // Dev account (moafunk_tester)
    pub instagram_access_token_dev: Option<String>,
    pub instagram_business_account_id_dev: Option<String>,
    // Prod account (moafunk_radio)
    pub instagram_access_token_prod: Option<String>,
    pub instagram_business_account_id_prod: Option<String>,
    // Facebook App credentials for token refresh
    pub facebook_app_id: Option<String>,
    pub facebook_app_secret: Option<String>,

    // OpenAI API settings (optional - for AI-generated artist bios)
    pub openai_api_key: Option<String>,

    // SoundCloud API settings (optional - for automatic upload of show recordings)
    pub soundcloud_client_id: Option<String>,
    pub soundcloud_client_secret: Option<String>,
    pub soundcloud_access_token: Option<String>,
    /// OAuth redirect URI (defaults to http://localhost:8000/api/soundcloud/callback)
    #[serde(default = "default_soundcloud_redirect_uri")]
    pub soundcloud_redirect_uri: String,

    // Telegram Bot settings (optional - for admin notifications and control)
    pub telegram_bot_token: Option<String>,
    pub telegram_admin_chat_id: Option<i64>,
    #[serde(default = "default_telegram_topic_id")]
    pub telegram_topic_id: Option<i32>,
    pub telegram_instagram_account: Option<String>,
    /// Hour of day (0-23) in Berlin time when artist Instagram previews should be sent
    #[serde(default = "default_telegram_artist_preview_hour")]
    pub telegram_artist_preview_hour: u32,

    // Admin panel base URL (used in Telegram notifications to link to artist profiles)
    #[serde(default = "default_admin_base_url")]
    pub admin_base_url: String,
}

fn default_host() -> String {
    "0.0.0.0".to_string()
}

fn default_port() -> u16 {
    8000
}

fn default_superadmin_username() -> String {
    "superadmin".to_string()
}

fn default_database_url() -> String {
    "sqlite:./data/unheard.db?mode=rwc".to_string()
}

fn default_max_file_size() -> u64 {
    180
}

fn default_max_upload_size() -> u64 {
    250
}

fn default_bucket_name() -> String {
    "unheard-artists-dev".to_string()
}

fn default_stream_output() -> String {
    "rtmp".to_string()
}

fn default_rtmp_url() -> String {
    "rtmp://stream.moafunk.de/live".to_string()
}

fn default_rtmp_stream_key() -> String {
    "stream-io".to_string()
}

fn default_icecast_bitrate() -> String {
    // 128 kbps MP3 — the codec/bitrate validated on iOS in the Phase-2 harness.
    "128k".to_string()
}

fn default_icecast_sample_rate() -> u32 {
    44100
}

fn default_icecast_metrics_poll_secs() -> u64 {
    // ~10-15s keeps the admin listener count near-live without hammering Icecast.
    15
}

fn default_ffmpeg_bitrate() -> String {
    "320k".to_string()
}

fn default_ffmpeg_sample_rate() -> u32 {
    44100
}

fn default_github_repo() -> String {
    "phaabe/live.moafunk.de".to_string()
}

fn default_telegram_topic_id() -> Option<i32> {
    // Moafunk Telegram channel topic ID "MoafunkBot" (see topic info for Id)
    Some(26)
}

fn default_telegram_artist_preview_hour() -> u32 {
    16 // 4:00 PM Berlin time
}

fn default_admin_base_url() -> String {
    "https://admin.live.moafunk.de".to_string()
}

fn default_soundcloud_redirect_uri() -> String {
    "http://localhost:8000/api/soundcloud/callback".to_string()
}

impl Config {
    pub fn from_env() -> Result<Self, envy::Error> {
        let mut config: Config = envy::from_env()?;
        config.r2_endpoint = format!("https://{}.r2.cloudflarestorage.com", config.r2_account_id);

        // Fail fast on a misconfigured producer rather than silently falling back
        // to RTMP (which would punch the live stream off the intended mount).
        validate_stream_output(&config.stream_output, config.icecast_url.as_deref())
            .map_err(envy::Error::custom)?;

        Ok(config)
    }

    pub fn max_file_size_bytes(&self) -> u64 {
        self.max_file_size_mb * 1024 * 1024
    }

    pub fn max_upload_size_bytes(&self) -> u64 {
        self.max_upload_size_mb * 1024 * 1024
    }

    pub fn max_request_body_bytes(&self) -> usize {
        // Allow some overhead for multipart boundaries/headers.
        ((self.max_upload_size_mb + 10) * 1024 * 1024) as usize
    }

    pub fn artist_logo_dir_path(&self) -> &str {
        self.artist_logo_dir
            .as_deref()
            .unwrap_or("./assets/artist_logos")
    }

    pub fn default_logo_path_path(&self) -> &str {
        self.default_logo_path
            .as_deref()
            .unwrap_or("./assets/brand/moafunk.png")
    }

    pub fn overlay_font_path_path(&self) -> Option<&str> {
        self.overlay_font_path.as_deref()
    }

    pub fn rtmp_destination(&self) -> String {
        format!("{}/{}", self.rtmp_url, self.rtmp_stream_key)
    }

    /// Where the live producer ffmpeg pushes the encoded audio. Validated at load
    /// (see [`Config::from_env`]), so this is infallible: `icecast` is only
    /// returned when `icecast_url` is present.
    pub fn producer_target(&self) -> crate::stream_bridge::PushTarget {
        use crate::stream_bridge::PushTarget;
        match self.stream_output.as_str() {
            "icecast" => PushTarget::Icecast {
                url: self.icecast_url.clone().unwrap_or_default(),
                bitrate: self.icecast_bitrate.clone(),
                sample_rate: self.icecast_sample_rate,
            },
            // Default + "rtmp": validated to be the only other accepted value.
            _ => PushTarget::Rtmp {
                destination: self.rtmp_destination(),
            },
        }
    }

    pub fn telegram_instagram_account(&self) -> &str {
        self.telegram_instagram_account.as_deref().unwrap_or("prod")
    }

    /// Icecast `/status-json.xsl` URL for the metrics poller (#177): the explicit
    /// `icecast_status_url` if set, otherwise derived from `icecast_url`. `None`
    /// disables the poller.
    pub fn icecast_status_url(&self) -> Option<String> {
        if let Some(u) = self.icecast_status_url.as_deref() {
            let t = u.trim();
            if !t.is_empty() {
                return Some(t.to_string());
            }
        }
        self.icecast_url.as_deref().and_then(derive_status_url)
    }
}

/// Derive `http://host:port/status-json.xsl` from a source URL like
/// `icecast://user:pass@host:port/mount`. Returns `None` if the host:port can't
/// be isolated.
fn derive_status_url(icecast_url: &str) -> Option<String> {
    let after_scheme = icecast_url.split_once("://").map(|(_, r)| r)?;
    // Drop optional `user:pass@` credentials.
    let after_creds = after_scheme
        .rsplit_once('@')
        .map(|(_, r)| r)
        .unwrap_or(after_scheme);
    let host_port = after_creds.split('/').next()?;
    if host_port.is_empty() {
        return None;
    }
    Some(format!("http://{host_port}/status-json.xsl"))
}

/// Validate the producer output selection. `icecast` requires a non-empty
/// `ICECAST_URL`; any value other than `rtmp`/`icecast` is rejected. Pure (no
/// env access) so it's unit-testable without building a full [`Config`].
fn validate_stream_output(stream_output: &str, icecast_url: Option<&str>) -> Result<(), String> {
    match stream_output {
        "rtmp" => Ok(()),
        "icecast" => {
            if icecast_url.map(str::trim).unwrap_or("").is_empty() {
                Err("STREAM_OUTPUT=icecast requires ICECAST_URL (icecast://source:pw@host:port/mount)"
                    .to_string())
            } else {
                Ok(())
            }
        }
        other => Err(format!(
            "invalid STREAM_OUTPUT '{other}' (expected 'rtmp' or 'icecast')"
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rtmp_output_is_valid_without_icecast_url() {
        assert!(validate_stream_output("rtmp", None).is_ok());
    }

    #[test]
    fn icecast_output_requires_a_url() {
        assert!(validate_stream_output("icecast", None).is_err());
        assert!(validate_stream_output("icecast", Some("   ")).is_err());
        assert!(validate_stream_output(
            "icecast",
            Some("icecast://source:pw@127.0.0.1:8010/live.mp3")
        )
        .is_ok());
    }

    #[test]
    fn unknown_output_is_rejected() {
        assert!(validate_stream_output("srt", Some("x")).is_err());
        assert!(validate_stream_output("", None).is_err());
    }

    #[test]
    fn status_url_derived_from_source_url() {
        assert_eq!(
            derive_status_url("icecast://source:pw@127.0.0.1:8010/live.mp3").as_deref(),
            Some("http://127.0.0.1:8010/status-json.xsl")
        );
        // No credentials in the URL.
        assert_eq!(
            derive_status_url("icecast://radio.example.com:8000/test.mp3").as_deref(),
            Some("http://radio.example.com:8000/status-json.xsl")
        );
    }

    #[test]
    fn status_url_derivation_rejects_malformed() {
        assert_eq!(derive_status_url("no-scheme"), None);
        assert_eq!(derive_status_url("icecast://"), None);
    }
}
