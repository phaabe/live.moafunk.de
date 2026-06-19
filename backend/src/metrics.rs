//! Prometheus text-format metrics (`GET /metrics`, issue #178).
//!
//! The external observability stack (Blackbox + `icecast_exporter`) can see the
//! public mount and listener counts, but it is **blind to backend-internal
//! durability failures**: a recorder whose tee queue overflowed, a segment
//! concat that failed, or an R2 upload that never landed all leave the live
//! mount looking perfectly healthy. This endpoint exposes those signals as
//! Prometheus counters so #178 can alert on a *stuck recording*, not just a dead
//! stream.
//!
//! Counters are process-global atomics (incremented at the failure sites, which
//! don't all hold `AppState`); gauges are rendered from the live stream/recording
//! state and the cached Icecast snapshot at scrape time.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::LazyLock;

use crate::stream_metrics::StreamMetrics;

/// Process-global monotonic counters. One instance, lazily initialized.
#[derive(Default)]
pub struct Counters {
    /// Tee chunks dropped because the recording queue was full (recorder/disk
    /// too slow). Each increment means the archive for that show is incomplete.
    pub recording_tee_dropped_total: AtomicU64,
    /// The recording writer task stopped mid-stream (channel closed) → archive
    /// abandoned for the rest of the show.
    pub recording_writer_stopped_total: AtomicU64,
    /// Segment concat (finalize) failed — segments exist on disk but the final
    /// artifact could not be assembled.
    pub recording_segment_concat_failures_total: AtomicU64,
    /// A recording multipart upload to R2 failed (and was aborted). The local
    /// file is kept (never deleted unverified).
    pub recording_r2_upload_failures_total: AtomicU64,
    /// Verify-before-delete found a local/remote size mismatch after upload.
    pub recording_size_mismatch_total: AtomicU64,
    /// A finalized recording was flagged incomplete for any reason (overflow,
    /// concat failure, size mismatch, short duration).
    pub recording_incomplete_total: AtomicU64,
}

static COUNTERS: LazyLock<Counters> = LazyLock::new(Counters::default);

/// Access the process-global counters (mainly for tests/rendering).
pub fn counters() -> &'static Counters {
    &COUNTERS
}

// Named increment helpers — call these from the failure sites.
pub fn inc_recording_tee_dropped() {
    COUNTERS
        .recording_tee_dropped_total
        .fetch_add(1, Ordering::Relaxed);
}
pub fn inc_recording_writer_stopped() {
    COUNTERS
        .recording_writer_stopped_total
        .fetch_add(1, Ordering::Relaxed);
}
pub fn inc_recording_segment_concat_failure() {
    COUNTERS
        .recording_segment_concat_failures_total
        .fetch_add(1, Ordering::Relaxed);
}
pub fn inc_recording_r2_upload_failure() {
    COUNTERS
        .recording_r2_upload_failures_total
        .fetch_add(1, Ordering::Relaxed);
}
pub fn inc_recording_size_mismatch() {
    COUNTERS
        .recording_size_mismatch_total
        .fetch_add(1, Ordering::Relaxed);
}
pub fn inc_recording_incomplete() {
    COUNTERS
        .recording_incomplete_total
        .fetch_add(1, Ordering::Relaxed);
}

/// Live runtime state captured at scrape time for the gauge metrics.
pub struct Gauges {
    pub stream_active: bool,
    pub recording_active: bool,
}

/// Escape a Prometheus label value (`\`, `"`, and newlines per the exposition
/// format spec). Mount paths are tame, but escape defensively.
fn escape_label(v: &str) -> String {
    v.replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
}

fn bool_gauge(out: &mut String, name: &str, help: &str, value: bool) {
    out.push_str(&format!("# HELP {name} {help}\n# TYPE {name} gauge\n"));
    out.push_str(&format!("{name} {}\n", if value { 1 } else { 0 }));
}

fn counter_line(out: &mut String, name: &str, help: &str, value: u64) {
    out.push_str(&format!("# HELP {name} {help}\n# TYPE {name} counter\n"));
    out.push_str(&format!("{name} {value}\n"));
}

/// Render the Prometheus exposition body. `now_ms` lets the poll-age gauge be
/// computed deterministically (and tested).
pub fn render(metrics: &StreamMetrics, gauges: &Gauges, now_ms: i64) -> String {
    let c = counters();
    let mut out = String::with_capacity(2048);

    // ── Backend-internal durability counters (the point of this endpoint) ──
    counter_line(
        &mut out,
        "moafunk_recording_tee_dropped_total",
        "Recording tee chunks dropped due to a full queue (archive incomplete).",
        c.recording_tee_dropped_total.load(Ordering::Relaxed),
    );
    counter_line(
        &mut out,
        "moafunk_recording_writer_stopped_total",
        "Recording writer task stopped mid-stream (archive abandoned).",
        c.recording_writer_stopped_total.load(Ordering::Relaxed),
    );
    counter_line(
        &mut out,
        "moafunk_recording_segment_concat_failures_total",
        "Recording segment concat (finalize) failures.",
        c.recording_segment_concat_failures_total
            .load(Ordering::Relaxed),
    );
    counter_line(
        &mut out,
        "moafunk_recording_r2_upload_failures_total",
        "Recording multipart uploads to R2 that failed and were aborted.",
        c.recording_r2_upload_failures_total.load(Ordering::Relaxed),
    );
    counter_line(
        &mut out,
        "moafunk_recording_size_mismatch_total",
        "Recording uploads where the verified remote size did not match local.",
        c.recording_size_mismatch_total.load(Ordering::Relaxed),
    );
    counter_line(
        &mut out,
        "moafunk_recording_incomplete_total",
        "Finalized recordings flagged incomplete for any reason.",
        c.recording_incomplete_total.load(Ordering::Relaxed),
    );

    // ── Live state gauges ──
    bool_gauge(
        &mut out,
        "moafunk_stream_active",
        "1 when a live broadcast is currently being ingested.",
        gauges.stream_active,
    );
    bool_gauge(
        &mut out,
        "moafunk_recording_active",
        "1 when a recording session is currently active.",
        gauges.recording_active,
    );

    // ── Icecast snapshot (mirrors the cached /api/stream/metrics poll) ──
    bool_gauge(
        &mut out,
        "moafunk_icecast_online",
        "1 when the last Icecast poll succeeded with at least one source connected.",
        metrics.online,
    );
    out.push_str(
        "# HELP moafunk_icecast_listeners Total listeners across all mounts (last poll).\n",
    );
    out.push_str("# TYPE moafunk_icecast_listeners gauge\n");
    out.push_str(&format!(
        "moafunk_icecast_listeners {}\n",
        metrics.total_listeners
    ));

    // Per-mount listeners + bitrate, labeled by mount path.
    if !metrics.mounts.is_empty() {
        out.push_str(
            "# HELP moafunk_icecast_mount_listeners Listeners on a single Icecast mount.\n",
        );
        out.push_str("# TYPE moafunk_icecast_mount_listeners gauge\n");
        for m in &metrics.mounts {
            out.push_str(&format!(
                "moafunk_icecast_mount_listeners{{mount=\"{}\"}} {}\n",
                escape_label(&m.mount),
                m.listeners
            ));
        }
        out.push_str(
            "# HELP moafunk_icecast_mount_bitrate_kbps Source bitrate on a single Icecast mount.\n",
        );
        out.push_str("# TYPE moafunk_icecast_mount_bitrate_kbps gauge\n");
        for m in &metrics.mounts {
            if let Some(br) = m.bitrate {
                out.push_str(&format!(
                    "moafunk_icecast_mount_bitrate_kbps{{mount=\"{}\"}} {}\n",
                    escape_label(&m.mount),
                    br
                ));
            }
        }
    }

    // Seconds since the last successful Icecast poll (-1 if never polled). Lets
    // an alert catch a wedged poller distinct from a dead mount.
    let poll_age = metrics
        .polled_at_ms
        .map(|t| ((now_ms - t).max(0) as f64) / 1000.0)
        .unwrap_or(-1.0);
    out.push_str(
        "# HELP moafunk_icecast_last_poll_age_seconds Seconds since the last successful Icecast poll (-1 if never).\n",
    );
    out.push_str("# TYPE moafunk_icecast_last_poll_age_seconds gauge\n");
    out.push_str(&format!(
        "moafunk_icecast_last_poll_age_seconds {poll_age}\n"
    ));

    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stream_metrics::{MountMetrics, StreamMetrics};

    fn sample_metrics() -> StreamMetrics {
        StreamMetrics {
            online: true,
            total_listeners: 12,
            mounts: vec![
                MountMetrics {
                    mount: "/live.mp3".to_string(),
                    listeners: 7,
                    listener_peak: Some(20),
                    bitrate: Some(128),
                    samplerate: Some(44100),
                    channels: Some(2),
                    server_name: Some("Moafunk".to_string()),
                },
                MountMetrics {
                    mount: "/test.mp3".to_string(),
                    listeners: 5,
                    listener_peak: None,
                    bitrate: None,
                    samplerate: None,
                    channels: None,
                    server_name: None,
                },
            ],
            polled_at_ms: Some(1_000_000),
            error: None,
        }
    }

    #[test]
    fn render_emits_help_type_and_values() {
        let m = sample_metrics();
        let g = Gauges {
            stream_active: true,
            recording_active: false,
        };
        let out = render(&m, &g, 1_005_000); // 5s after the poll

        // Every metric carries HELP + TYPE.
        assert!(out.contains("# TYPE moafunk_recording_tee_dropped_total counter"));
        assert!(out.contains("# TYPE moafunk_stream_active gauge"));
        assert!(out.contains("# TYPE moafunk_icecast_mount_listeners gauge"));

        // Gauges reflect the inputs.
        assert!(out.contains("\nmoafunk_stream_active 1\n"));
        assert!(out.contains("\nmoafunk_recording_active 0\n"));
        assert!(out.contains("\nmoafunk_icecast_online 1\n"));
        assert!(out.contains("\nmoafunk_icecast_listeners 12\n"));

        // Per-mount labeled series.
        assert!(out.contains("moafunk_icecast_mount_listeners{mount=\"/live.mp3\"} 7"));
        assert!(out.contains("moafunk_icecast_mount_listeners{mount=\"/test.mp3\"} 5"));
        assert!(out.contains("moafunk_icecast_mount_bitrate_kbps{mount=\"/live.mp3\"} 128"));
        // No bitrate on /test.mp3 → no series for it.
        assert!(!out.contains("moafunk_icecast_mount_bitrate_kbps{mount=\"/test.mp3\"}"));

        // Poll age = 5s.
        assert!(out.contains("moafunk_icecast_last_poll_age_seconds 5\n"));
    }

    #[test]
    fn never_polled_reports_negative_age_and_offline() {
        let m = StreamMetrics::default(); // online=false, no polled_at_ms
        let g = Gauges {
            stream_active: false,
            recording_active: false,
        };
        let out = render(&m, &g, 9_999);
        assert!(out.contains("\nmoafunk_icecast_online 0\n"));
        assert!(out.contains("moafunk_icecast_last_poll_age_seconds -1\n"));
        // No mounts → no per-mount series emitted.
        assert!(!out.contains("moafunk_icecast_mount_listeners{"));
    }

    #[test]
    fn counters_increment_and_render() {
        // Snapshot the baseline (other tests share the process-global counters).
        let before = counters()
            .recording_tee_dropped_total
            .load(Ordering::Relaxed);
        inc_recording_tee_dropped();
        inc_recording_tee_dropped();
        let after = counters()
            .recording_tee_dropped_total
            .load(Ordering::Relaxed);
        assert_eq!(after, before + 2);

        let out = render(
            &StreamMetrics::default(),
            &Gauges {
                stream_active: false,
                recording_active: false,
            },
            0,
        );
        // The rendered counter line is present and parseable.
        let line = out
            .lines()
            .find(|l| l.starts_with("moafunk_recording_tee_dropped_total "))
            .expect("counter line present");
        let val: u64 = line.rsplit(' ').next().unwrap().parse().unwrap();
        assert_eq!(val, after);
    }

    #[test]
    fn label_values_are_escaped() {
        assert_eq!(escape_label("/live.mp3"), "/live.mp3");
        assert_eq!(escape_label(r#"a"b\c"#), r#"a\"b\\c"#);
    }
}
