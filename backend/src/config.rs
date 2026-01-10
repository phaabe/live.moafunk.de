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

    // RTMP streaming settings
    #[serde(default = "default_rtmp_url")]
    pub rtmp_url: String,
    #[serde(default = "default_rtmp_stream_key")]
    pub rtmp_stream_key: String,

    // Optional assets used for ZIP-time image stamping
    // If not set, the code will fall back to local paths under ./data.
    pub overlay_font_path: Option<String>,
    pub artist_logo_dir: Option<String>,
    pub default_logo_path: Option<String>,
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
    100
}

fn default_max_upload_size() -> u64 {
    250
}

fn default_bucket_name() -> String {
    "unheard-artists".to_string()
}

fn default_rtmp_url() -> String {
    "rtmp://stream.moafunk.de/live".to_string()
}

fn default_rtmp_stream_key() -> String {
    "stream-io".to_string()
}

impl Config {
    pub fn from_env() -> Result<Self, envy::Error> {
        let mut config: Config = envy::from_env()?;
        config.r2_endpoint = format!("https://{}.r2.cloudflarestorage.com", config.r2_account_id);
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
}
