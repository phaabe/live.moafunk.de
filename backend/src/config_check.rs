//! Data-free configuration validation for deployment and mail activation.

use crate::{handlers::recovery::RecoveryControls, recovery_mail::RecoveryMailer, Config};
use anyhow::{anyhow, Result};
use std::{cell::Cell, collections::HashSet, env, fs, io, io::Read};

pub fn run() -> Result<()> {
    // envy uses env::vars(), which panics with the offending value when an
    // inherited key or value is not Unicode. Refuse it without that panic.
    anyhow::ensure!(
        env::vars_os().all(|(key, value)| key.to_str().is_some() && value.to_str().is_some()),
        "configuration environment must contain valid Unicode"
    );
    // Tokio consumes this from the inherited environment before dotenv loads
    // during normal startup. Check it without constructing the runtime, whose
    // parse panic includes the supplied value.
    if let Ok(workers) = env::var("TOKIO_WORKER_THREADS") {
        anyhow::ensure!(
            workers.parse::<usize>().is_ok_and(|workers| workers > 0),
            "TOKIO_WORKER_THREADS must be a positive integer"
        );
    }
    if let Some(source) = environment_file()? {
        load_strict(&source)?;
    }

    // Config errors can contain values (for example envy numeric parse errors).
    // Do not retain them as causes: anyhow may print the entire cause chain.
    Config::from_env().map_err(|_| anyhow!("invalid core configuration"))?;
    let mail =
        RecoveryMailer::from_env().map_err(|_| anyhow!("invalid recovery mail configuration"))?;
    RecoveryControls::from_env()
        .map_err(|_| anyhow!("RECOVERY_TRUST_PROXY must be true or false"))?;
    println!(
        "Configuration valid (recovery mail: {})",
        if mail.enabled() {
            "enabled"
        } else {
            "disabled"
        }
    );
    Ok(())
}

/// Match dotenvy's search: the nearest regular .env file in this directory or
/// an ancestor. A missing file is valid when the process supplies configuration.
fn environment_file() -> Result<Option<String>> {
    let directory = env::current_dir().map_err(|_| anyhow!("cannot locate dotenv file"))?;
    for directory in directory.ancestors() {
        let path = directory.join(".env");
        match fs::metadata(&path) {
            Ok(metadata) if metadata.is_file() => {
                return fs::read_to_string(path)
                    .map(Some)
                    .map_err(|_| anyhow!("cannot read dotenv file"));
            }
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(_) => return Err(anyhow!("cannot inspect dotenv file")),
        }
    }
    Ok(None)
}

fn load_strict(source: &str) -> Result<()> {
    // Iter::load strips a BOM; from_read_iter does not.
    let source = source.strip_prefix('\u{feff}').unwrap_or(source);
    let offset = Cell::new(0);
    let mut entries = dotenvy::from_read_iter(PhysicalLines {
        bytes: source.as_bytes(),
        offset: &offset,
    });
    let mut seen = HashSet::new();
    loop {
        let start = offset.get();
        let Some(entry) = entries.next() else {
            return Ok(());
        };
        let line = source[..start].bytes().filter(|b| *b == b'\n').count()
            + source[start..offset.get()]
                .lines()
                .position(|line| !line.trim().is_empty() && !line.trim_start().starts_with('#'))
                .unwrap_or(0)
            + 1;
        // LineParse contains raw input, and its offset is not a physical line
        // number. Neither Display nor Debug is safe to include in diagnostics.
        let (key, value) =
            entry.map_err(|_| anyhow!("invalid dotenv entry starting at line {line}"))?;
        anyhow::ensure!(
            !value.contains('\0'),
            "invalid dotenv entry starting at line {line}"
        );
        if !seen.insert(key.clone()) {
            eprintln!("warning: duplicate dotenv key {key}; first definition wins");
        }
        // Set each entry immediately, like Iter::load: later substitutions see
        // the first file definition, while the process environment wins.
        if env::var(&key).is_err() {
            env::set_var(key, value);
        }
    }
}

/// Prevent the parser's BufReader from reading ahead beyond a physical line.
/// One iterator still handles multiline quotes and variable substitutions.
struct PhysicalLines<'a> {
    bytes: &'a [u8],
    offset: &'a Cell<usize>,
}

impl Read for PhysicalLines<'_> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        let remaining = &self.bytes[self.offset.get()..];
        let length = remaining
            .iter()
            .position(|b| *b == b'\n')
            .map_or(remaining.len(), |position| position + 1)
            .min(buffer.len());
        buffer[..length].copy_from_slice(&remaining[..length]);
        self.offset.set(self.offset.get() + length);
        Ok(length)
    }
}
