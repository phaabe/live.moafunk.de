use serde::Deserialize;

#[derive(Clone, Debug, Deserialize)]
pub struct Config {
    #[serde(default = "default_host")]
    pub host: String,
    #[serde(default = "default_port")]
    pub port: u16,

    pub secret_key: String,
    pub admin_password_hash: String,

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
}

fn default_host() -> String {
    "0.0.0.0".to_string()
}

fn default_port() -> u16 {
    8000
}

fn default_database_url() -> String {
    "sqlite:./data/unheard.db?mode=rwc".to_string()
}

fn default_max_file_size() -> u64 {
    50
}

fn default_max_upload_size() -> u64 {
    100
}

fn default_bucket_name() -> String {
    "unheard-artists".to_string()
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
}
