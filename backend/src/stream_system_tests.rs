//! System tests of the live stream chain: the real stream WebSocket handler
//! and producer ffmpeg push a tone into a running Liquidsoap harbor, and the
//! tests decode what listeners get from Icecast.
//!
//! Ignored by default because they need the stream stack. The `stream-system`
//! CI job starts Liquidsoap and Icecast-KH with the production configs from
//! `docs/stream-rework/prod/`. To run them locally, start both the same way,
//! then:
//!
//! ```text
//! cargo test stream_system_tests -- --ignored --test-threads=1
//! ```
//!
//! Environment, named as in `stream.env`:
//! - `HARBOR_LIVE_PASSWORD`, `HARBOR_TEST_PASSWORD`: the harbor passwords
//!   `moafunk.liq` was started with (required).
//! - `STREAM_SYSTEM_HARBOR`: harbor `host:port`, default `127.0.0.1:8005`.
//! - `STREAM_SYSTEM_ICECAST`: Icecast base URL, default `http://127.0.0.1:8010`.

use crate::broadcast_tests::{add_user, go_live, serve_stream_socket_with, Producer};
use futures_util::stream::{SplitSink, SplitStream};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::process::Stdio;
use std::time::{Duration, Instant};
use tokio::io::AsyncReadExt;
use tokio::process::Command;
use tokio_tungstenite::tungstenite::Message as WsMessage;

/// A 440 Hz tone after Liquidsoap's MP3 encode measures about -24 dB mean.
const TONE_DB: f64 = -40.0;
/// The silence `mksafe` plays while no producer is connected measures -91 dB.
const SILENCE_DB: f64 = -70.0;
/// The harbor buffers several seconds before a new source is heard.
const HEAR_WITHIN: Duration = Duration::from_secs(45);

struct Stack {
    harbor: String,
    icecast: String,
    live_password: String,
    test_password: String,
}

impl Stack {
    fn from_env() -> Self {
        let required = |name: &str| {
            std::env::var(name)
                .unwrap_or_else(|_| panic!("set {name} to the harbor password in moafunk.liq"))
        };
        Self {
            harbor: std::env::var("STREAM_SYSTEM_HARBOR")
                .unwrap_or_else(|_| "127.0.0.1:8005".into()),
            icecast: std::env::var("STREAM_SYSTEM_ICECAST")
                .unwrap_or_else(|_| "http://127.0.0.1:8010".into()),
            live_password: required("HARBOR_LIVE_PASSWORD"),
            test_password: required("HARBOR_TEST_PASSWORD"),
        }
    }

    /// The backend's producer settings for this stack, as in production.
    fn config(&self) -> Value {
        json!({
            "stream_output": "icecast",
            "icecast_url": format!("icecast://source:{}@{}/live", self.live_password, self.harbor),
            "icecast_test_url": format!("icecast://source:{}@{}/test", self.test_password, self.harbor),
        })
    }

    fn mount(&self, name: &str) -> String {
        format!("{}/{name}", self.icecast)
    }
}

/// A tone streamed as WebM/Opus chunks, as the panel's MediaRecorder sends it.
struct Tone {
    stop: tokio::sync::oneshot::Sender<()>,
    task: tokio::task::JoinHandle<SplitSink<Producer, WsMessage>>,
}

impl Tone {
    fn start(mut sink: SplitSink<Producer, WsMessage>) -> Self {
        let (stop, mut stopped) = tokio::sync::oneshot::channel();
        let task = tokio::spawn(async move {
            let mut ffmpeg = Command::new("ffmpeg")
                .args(["-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi"])
                .args(["-i", "sine=frequency=440:sample_rate=48000", "-ac", "2"])
                .args(["-c:a", "libopus", "-b:a", "128k", "-f", "webm"])
                .args(["-cluster_time_limit", "250", "pipe:1"])
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .kill_on_drop(true)
                .spawn()
                .expect("start the tone ffmpeg");
            let mut webm = ffmpeg.stdout.take().unwrap();
            let mut chunk = vec![0u8; 16 * 1024];
            loop {
                tokio::select! {
                    _ = &mut stopped => break,
                    read = webm.read(&mut chunk) => match read {
                        Ok(n) if n > 0 => {
                            if sink.send(WsMessage::Binary(chunk[..n].to_vec())).await.is_err() {
                                break;
                            }
                        }
                        _ => break,
                    },
                }
            }
            let _ = ffmpeg.kill().await;
            sink
        });
        Self { stop, task }
    }

    /// Stops the tone and ends the broadcast the way the panel's Stop does.
    async fn stop_broadcast(self, mut events: SplitStream<Producer>) {
        let _ = self.stop.send(());
        let mut sink = self.task.await.unwrap();
        sink.send(WsMessage::Text("stop".into())).await.unwrap();
        // The server closes the socket once its disconnect path has run.
        let drained = async { while let Some(Ok(_)) = events.next().await {} };
        tokio::time::timeout(Duration::from_secs(20), drained)
            .await
            .expect("server kept the socket open after stop");
    }
}

/// What a listener got from a mount.
struct Heard {
    /// ffmpeg's description of the received stream (codec, rate, layout, bitrate).
    stream: String,
    mean_db: f64,
}

/// Decodes `seconds` of a mount, as a listener would.
async fn listen(url: &str, seconds: u32) -> Heard {
    let run = Command::new("ffmpeg")
        .args([
            "-hide_banner",
            "-nostats",
            "-t",
            &seconds.to_string(),
            "-i",
            url,
        ])
        .args(["-af", "volumedetect", "-f", "null", "-"])
        .stdin(Stdio::null())
        .kill_on_drop(true)
        .output();
    let out = tokio::time::timeout(Duration::from_secs(u64::from(seconds) + 20), run)
        .await
        .unwrap_or_else(|_| panic!("listening to {url} hung"))
        .expect("start the listener ffmpeg");
    let log = String::from_utf8_lossy(&out.stderr);
    assert!(out.status.success(), "could not decode {url}:\n{log}");
    let stream = log
        .lines()
        .find(|line| line.contains("Audio:"))
        .unwrap_or_default()
        .trim()
        .to_string();
    let mean_db = log
        .lines()
        .find_map(|line| line.split("mean_volume:").nth(1))
        .and_then(|value| value.trim().trim_end_matches("dB").trim().parse().ok())
        .unwrap_or_else(|| panic!("no volume reading for {url}:\n{log}"));
    Heard { stream, mean_db }
}

/// Listens until the mount plays the tone (or silence), or gives up after
/// [`HEAR_WITHIN`]. Returns the last reading.
async fn listen_until(url: &str, tone: bool) -> Heard {
    let deadline = Instant::now() + HEAR_WITHIN;
    loop {
        let heard = listen(url, 3).await;
        let done = if tone {
            heard.mean_db > TONE_DB
        } else {
            heard.mean_db < SILENCE_DB
        };
        if done || Instant::now() > deadline {
            return heard;
        }
    }
}

#[tokio::test]
#[ignore = "needs Liquidsoap and Icecast; run by the stream-system CI job"]
async fn live_broadcast_reaches_listeners_and_ends_in_silence() {
    let stack = Stack::from_env();
    let (state, _temp, addr) = serve_stream_socket_with(stack.config()).await;
    let admin = add_user(&state, "admin", "admin").await;
    let live = stack.mount("live.mp3");

    let (sink, events) = go_live(&state, addr, admin, "").await.split();
    let tone = Tone::start(sink);
    let heard = listen_until(&live, true).await;
    assert!(
        heard.mean_db > TONE_DB,
        "the broadcast never reached /live.mp3 (mean {} dB)",
        heard.mean_db
    );
    // Listeners get the production encode, which iOS Safari can play.
    for part in ["mp3", "44100 Hz", "stereo", "256 kb/s"] {
        assert!(
            heard.stream.contains(part),
            "/live.mp3 is not {part}: {}",
            heard.stream
        );
    }
    assert!(state.stream_state.lock().await.get_status().active);

    // Stopping ends the broadcast: the mount stays up and plays silence.
    tone.stop_broadcast(events).await;
    let heard = listen_until(&live, false).await;
    assert!(
        heard.mean_db < SILENCE_DB,
        "/live.mp3 still plays audio after the stop (mean {} dB)",
        heard.mean_db
    );
    assert!(!state.stream_state.lock().await.get_status().active);
}

#[tokio::test]
#[ignore = "needs Liquidsoap and Icecast; run by the stream-system CI job"]
async fn rehearsal_reaches_the_test_mount_and_never_the_public_one() {
    let stack = Stack::from_env();
    let (state, _temp, addr) = serve_stream_socket_with(stack.config()).await;
    let host = add_user(&state, "host", "host").await;

    let (sink, events) = go_live(&state, addr, host, "test=true").await.split();
    let tone = Tone::start(sink);
    let heard = listen_until(&stack.mount("test.mp3"), true).await;
    assert!(
        heard.mean_db > TONE_DB,
        "the rehearsal never reached /test.mp3 (mean {} dB)",
        heard.mean_db
    );
    // By now a leak would be audible on the public mount too.
    let public = listen(&stack.mount("live.mp3"), 3).await;
    assert!(
        public.mean_db < SILENCE_DB,
        "the rehearsal is audible on /live.mp3 (mean {} dB)",
        public.mean_db
    );
    // The public player stays off air during a rehearsal.
    assert!(!state.stream_state.lock().await.get_status().active);

    tone.stop_broadcast(events).await;
}
