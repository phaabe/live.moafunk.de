use std::fs;
use std::path::Path;
use std::process::{Command, Output};

fn check_config(directory: &Path, environment: &[(&str, &str)]) -> Output {
    command(directory, environment)
        .arg("check-config")
        .output()
        .expect("run check-config")
}

fn command(directory: &Path, environment: &[(&str, &str)]) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_unheard-backend"));
    command
        .current_dir(directory)
        .env_clear()
        .env("SECRET_KEY", "fixture-secret-key")
        .env("SUPERADMIN_PASSWORD_HASH", "fixture-password-hash")
        .env("R2_ACCOUNT_ID", "fixture-account")
        .env("R2_ACCESS_KEY_ID", "fixture-access-key")
        .env("R2_SECRET_ACCESS_KEY", "fixture-r2-secret")
        .env(
            "DATABASE_URL",
            "invalid-database://check-config-must-not-connect",
        )
        .envs(environment.iter().copied());
    command
}

fn text(output: &Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}

fn assert_valid(output: &Output, enabled: bool) {
    assert!(output.status.success(), "{}", text(output));
    let state = if enabled { "enabled" } else { "disabled" };
    assert_eq!(
        String::from_utf8_lossy(&output.stdout).trim(),
        format!("Configuration valid (recovery mail: {state})")
    );
}

fn assert_invalid_without(output: &Output, sensitive_values: &[&str]) {
    assert!(!output.status.success(), "{}", text(output));
    let diagnostics = text(output);
    assert!(!diagnostics.contains("panicked"), "{diagnostics}");
    for value in sensitive_values {
        assert!(
            !diagnostics.contains(value),
            "leaked fixture value: {value}"
        );
    }
}

#[test]
fn absent_mail_configuration_passes_without_database_or_data_files() {
    let directory = tempfile::tempdir().unwrap();

    let output = check_config(directory.path(), &[]);

    assert_valid(&output, false);
    assert!(output.stderr.is_empty(), "{}", text(&output));
    assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 0);
}

#[test]
fn complete_mail_configuration_passes_without_sending_mail() {
    let directory = tempfile::tempdir().unwrap();

    let output = check_config(
        directory.path(),
        &[
            ("RECOVERY_MAIL_API_KEY", "fixture-mail-api-key"),
            ("RECOVERY_MAIL_FROM", "Moafunk <noreply@example.invalid>"),
            ("RECOVERY_PUBLIC_URL", "https://admin.example.invalid/"),
            ("RECOVERY_TRUST_PROXY", "true"),
        ],
    );

    assert_valid(&output, true);
    assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 0);
}

#[test]
fn unquoted_sender_with_spaces_is_rejected_without_its_value() {
    let directory = tempfile::tempdir().unwrap();
    fs::write(
        directory.path().join(".env"),
        "# sender\nRECOVERY_MAIL_FROM=Private Sender <private@example.invalid>\n",
    )
    .unwrap();

    let output = check_config(directory.path(), &[]);

    assert_invalid_without(&output, &["Private Sender", "private@example.invalid"]);
    assert!(
        text(&output).contains("invalid dotenv entry starting at line 2"),
        "{}",
        text(&output)
    );
}

#[test]
fn malformed_secret_is_rejected_without_lineparse_text() {
    let directory = tempfile::tempdir().unwrap();
    fs::write(
        directory.path().join(".env"),
        "# first line\nRECOVERY_MAIL_API_KEY='private-malformed-secret\n",
    )
    .unwrap();

    let output = check_config(directory.path(), &[]);

    assert_invalid_without(&output, &["private-malformed-secret", "LineParse"]);
    assert!(
        text(&output).contains("invalid dotenv entry starting at line 2"),
        "{}",
        text(&output)
    );
}

#[test]
fn partial_mail_configuration_is_rejected_without_api_key() {
    let directory = tempfile::tempdir().unwrap();

    let output = check_config(
        directory.path(),
        &[("RECOVERY_MAIL_API_KEY", "private-partial-mail-key")],
    );

    assert_invalid_without(&output, &["private-partial-mail-key"]);
    assert!(text(&output).contains("recovery"), "{}", text(&output));
}

#[test]
fn numeric_trust_proxy_is_rejected_even_when_mail_is_disabled() {
    let directory = tempfile::tempdir().unwrap();

    let output = check_config(directory.path(), &[("RECOVERY_TRUST_PROXY", "1")]);

    assert_invalid_without(&output, &[]);
    assert!(
        text(&output).contains("RECOVERY_TRUST_PROXY"),
        "{}",
        text(&output)
    );
}

#[test]
fn duplicates_warn_with_key_only_and_keep_the_first_definition() {
    let directory = tempfile::tempdir().unwrap();
    fs::write(
        directory.path().join(".env"),
        "PORT=8123\nPORT=private-invalid-port\n",
    )
    .unwrap();

    let output = check_config(directory.path(), &[]);

    assert_valid(&output, false);
    assert_eq!(
        String::from_utf8_lossy(&output.stderr).trim(),
        "warning: duplicate dotenv key PORT; first definition wins"
    );
    assert!(!text(&output).contains("private-invalid-port"));
    assert!(!text(&output).contains("8123"));
}

#[test]
fn process_environment_takes_precedence_over_the_first_file_definition() {
    let directory = tempfile::tempdir().unwrap();
    fs::write(
        directory.path().join(".env"),
        "PORT=invalid-first-port\nPORT=invalid-second-port\nMAX_FILE_SIZE_MB=${PORT}\n",
    )
    .unwrap();

    let output = check_config(directory.path(), &[("PORT", "8123")]);

    assert_valid(&output, false);
    assert!(!text(&output).contains("invalid-first-port"));
    assert!(!text(&output).contains("invalid-second-port"));
}

#[test]
fn substitutions_use_the_first_file_definition_after_a_duplicate() {
    let directory = tempfile::tempdir().unwrap();
    fs::write(
        directory.path().join(".env"),
        "PORT=8123\nPORT=invalid-duplicate-port\nMAX_FILE_SIZE_MB=${PORT}\n",
    )
    .unwrap();

    let output = check_config(directory.path(), &[]);

    assert_valid(&output, false);
    assert!(!text(&output).contains("invalid-duplicate-port"));
}

#[test]
fn malformed_duplicate_fails_even_when_process_environment_overrides_it() {
    let directory = tempfile::tempdir().unwrap();
    fs::write(
        directory.path().join(".env"),
        "PORT=8123\nPORT=private malformed duplicate\n",
    )
    .unwrap();

    let output = check_config(directory.path(), &[("PORT", "8124")]);

    assert_invalid_without(&output, &["private malformed duplicate"]);
    assert!(
        text(&output).contains("invalid dotenv entry starting at line 2"),
        "{}",
        text(&output)
    );
}

#[test]
fn quoted_values_multiline_values_bom_and_crlf_are_accepted() {
    let directory = tempfile::tempdir().unwrap();
    fs::write(
        directory.path().join(".env"),
        "\u{feff}RECOVERY_MAIL_API_KEY='fixture-mail-key'\r\n\
         RECOVERY_MAIL_FROM=\"Moafunk <noreply@example.invalid>\"\r\n\
         RECOVERY_PUBLIC_URL=https://admin.example.invalid/\r\n\
         OPTIONAL_MULTILINE=\"first line\r\nsecond line\"\r\n",
    )
    .unwrap();

    let output = check_config(directory.path(), &[]);

    assert_valid(&output, true);
    assert!(output.stderr.is_empty(), "{}", text(&output));
}

#[test]
fn nul_in_parsed_value_is_rejected_without_panic_or_value() {
    let directory = tempfile::tempdir().unwrap();
    fs::write(
        directory.path().join(".env"),
        b"OPTIONAL_SECRET=private-nul-before\0private-nul-after\n",
    )
    .unwrap();

    let output = check_config(directory.path(), &[]);

    assert_invalid_without(&output, &["private-nul-before", "private-nul-after"]);
}

#[test]
fn invalid_core_typed_value_is_rejected_without_value() {
    let directory = tempfile::tempdir().unwrap();

    let output = check_config(directory.path(), &[("PORT", "private-invalid-core-port")]);

    assert_invalid_without(&output, &["private-invalid-core-port"]);
    assert!(!output.stderr.is_empty());
}

#[test]
fn invalid_recovery_url_is_rejected_without_url_credentials() {
    let directory = tempfile::tempdir().unwrap();

    let output = check_config(
        directory.path(),
        &[
            ("RECOVERY_MAIL_API_KEY", "private-url-mail-key"),
            ("RECOVERY_MAIL_FROM", "Moafunk <noreply@example.invalid>"),
            (
                "RECOVERY_PUBLIC_URL",
                "https://private-user:private-password@admin.example.invalid/",
            ),
        ],
    );

    assert_invalid_without(
        &output,
        &["private-url-mail-key", "private-user", "private-password"],
    );
}

#[test]
fn dotenv_is_loaded_from_the_parent_directory() {
    let directory = tempfile::tempdir().unwrap();
    fs::write(
        directory.path().join(".env"),
        "PORT=private-invalid-parent-port\n",
    )
    .unwrap();
    let child = directory.path().join("child");
    fs::create_dir(&child).unwrap();

    let output = check_config(&child, &[]);

    assert_invalid_without(&output, &["private-invalid-parent-port"]);
    assert_eq!(fs::read_dir(&child).unwrap().count(), 0);
}

#[test]
fn missing_required_core_configuration_is_rejected() {
    let directory = tempfile::tempdir().unwrap();

    let output = command(directory.path(), &[])
        .env_remove("SECRET_KEY")
        .arg("check-config")
        .output()
        .unwrap();

    assert_invalid_without(&output, &["fixture-password-hash", "fixture-r2-secret"]);
}

#[test]
fn extra_cli_arguments_are_rejected_without_echoing_them() {
    let directory = tempfile::tempdir().unwrap();

    let output = command(directory.path(), &[])
        .args(["check-config", "private-cli-value"])
        .output()
        .unwrap();

    assert_invalid_without(&output, &["private-cli-value"]);
    assert!(text(&output).contains("Usage:"), "{}", text(&output));
    assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 0);
}

#[cfg(unix)]
#[test]
fn non_unicode_inherited_value_is_rejected_without_panic_or_value() {
    use std::ffi::OsString;
    use std::os::unix::ffi::OsStringExt;

    let directory = tempfile::tempdir().unwrap();
    let mut value = b"private-nonunicode-secret".to_vec();
    value.push(0xff);

    let output = command(directory.path(), &[])
        .env("OPTIONAL_SECRET", OsString::from_vec(value))
        .arg("check-config")
        .output()
        .unwrap();

    assert_invalid_without(&output, &["private-nonunicode-secret"]);
}

#[test]
fn malformed_entry_after_multiline_value_reports_its_physical_line() {
    let directory = tempfile::tempdir().unwrap();
    fs::write(
        directory.path().join(".env"),
        "# before multiline\nOPTIONAL_MULTILINE=\"first line\nsecond line\"\n\n# before failure\nOPTIONAL_SECRET=private malformed value\n",
    )
    .unwrap();

    let output = check_config(directory.path(), &[]);

    assert_invalid_without(&output, &["private malformed value"]);
    assert!(
        text(&output).contains("invalid dotenv entry starting at line 6"),
        "{}",
        text(&output)
    );
}

#[test]
fn malformed_entry_after_long_line_reports_its_physical_line() {
    let directory = tempfile::tempdir().unwrap();
    let long_value = "x".repeat(20_000);
    fs::write(
        directory.path().join(".env"),
        format!("OPTIONAL_LONG={long_value}\nOPTIONAL_SECRET=private malformed value\n"),
    )
    .unwrap();

    let output = check_config(directory.path(), &[]);

    assert_invalid_without(&output, &["private malformed value", &long_value]);
    assert!(
        text(&output).contains("invalid dotenv entry starting at line 2"),
        "{}",
        text(&output)
    );
}

#[test]
fn invalid_inherited_tokio_worker_count_is_rejected_without_panic_or_value() {
    let directory = tempfile::tempdir().unwrap();

    for value in ["private-invalid-workers", "0"] {
        let output = check_config(directory.path(), &[("TOKIO_WORKER_THREADS", value)]);

        assert_invalid_without(&output, &[value]);
    }
}

#[test]
fn valid_inherited_tokio_worker_count_passes() {
    let directory = tempfile::tempdir().unwrap();

    let output = check_config(directory.path(), &[("TOKIO_WORKER_THREADS", "2")]);

    assert_valid(&output, false);
}

#[test]
fn dotenv_worker_count_is_ignored_as_it_is_during_runtime_initialization() {
    let directory = tempfile::tempdir().unwrap();
    fs::write(
        directory.path().join(".env"),
        "TOKIO_WORKER_THREADS=not-a-worker-count\n",
    )
    .unwrap();

    let output = check_config(directory.path(), &[]);

    assert_valid(&output, false);
}
