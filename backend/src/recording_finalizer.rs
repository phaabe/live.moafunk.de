//! When a live show's recording is finalized.
//!
//! A producer that drops without "stop" gets [`FINALIZE_GRACE`] to come back
//! before its recording is finalized, so a flaky connection resumes the same
//! archive. That wait can be cancelled; a finalize that has started cannot:
//! - The switch from waiting to finalizing and every cancellation take the
//!   same lock, so exactly one of them wins.
//! - Every finalize runs in its own task. No caller, and no dropped request,
//!   can abort it.
//! - Finalizes detach their sessions (close the tee, take the session) one
//!   after another, so no session is finalized twice.
//! - Before another show goes live, the waiting show's session is detached,
//!   so the new show's audio never reaches the old show's file.

use crate::handlers::recording::FinalizedRecording;
use futures_util::future::BoxFuture;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;
use tokio::sync::watch;
use tokio::task::AbortHandle;

/// Grace period after a live producer drops before its recording is finalized.
pub const FINALIZE_GRACE: Duration = Duration::from_secs(30);

/// Result of one finalize, shared with every caller that waits for it. The
/// error is text so each waiter gets its own copy.
pub type Outcome = Result<Option<FinalizedRecording>, String>;

/// The recording steps the state machine drives.
/// [`crate::handlers::recording::LiveFinalize`] is the real one.
pub trait FinalizeOps: Send + Sync + 'static {
    type Session: Send + 'static;

    /// Close the recording tee and take the current session. With `show`,
    /// only that show's session is taken; another show's stays untouched.
    fn detach(&self, show: Option<i64>) -> BoxFuture<'_, Result<Option<Self::Session>, String>>;

    /// Upload a detached session and record its version.
    fn upload(&self, session: Self::Session) -> BoxFuture<'_, Result<FinalizedRecording, String>>;

    /// True while a producer is live.
    fn stream_active(&self) -> BoxFuture<'_, bool>;
}

#[derive(Default)]
pub struct Finalizer {
    slot: Mutex<Slot>,
}

#[derive(Default)]
struct Slot {
    next_id: u64,
    /// The grace wait of a dropped show, if any.
    waiting: Option<Waiting>,
    /// Finalizes that have started, oldest first.
    running: Vec<Running>,
}

struct Waiting {
    id: u64,
    show_id: i64,
    /// The grace task, which may have finished its live probe. Aborted when
    /// the pending wait is cancelled or replaced.
    task: AbortHandle,
}

#[derive(Clone)]
struct Running {
    id: u64,
    detached: watch::Receiver<bool>,
    done: watch::Receiver<Option<Outcome>>,
}

/// The sending side of a registered finalize.
struct Started {
    id: u64,
    /// Detach signal of the previous finalize; this one detaches after it.
    after: Option<watch::Receiver<bool>>,
    detached: watch::Sender<bool>,
    done: watch::Sender<Option<Outcome>>,
}

impl Slot {
    /// Register a finalize. It detaches after the newest running one.
    fn start(&mut self) -> Started {
        self.next_id += 1;
        let (detached, detached_rx) = watch::channel(false);
        let (done, done_rx) = watch::channel(None);
        let after = self.running.last().map(|r| r.detached.clone());
        self.running.push(Running {
            id: self.next_id,
            detached: detached_rx,
            done: done_rx,
        });
        Started {
            id: self.next_id,
            after,
            detached,
            done,
        }
    }
}

/// Removes a finalize from the slot when its run ends, also on a panic.
struct Unregister<'a>(&'a Finalizer, u64);

impl Drop for Unregister<'_> {
    fn drop(&mut self) {
        self.0.slot().running.retain(|r| r.id != self.1);
    }
}

impl Finalizer {
    fn slot(&self) -> MutexGuard<'_, Slot> {
        // No code panics while holding the lock; recover the data if one did.
        self.slot.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// A live producer of `show_id` dropped: finalize after [`FINALIZE_GRACE`]
    /// unless a producer goes live first. Replaces an earlier wait. A running
    /// finalize is never touched.
    pub fn schedule<O: FinalizeOps>(self: &Arc<Self>, ops: Arc<O>, show_id: i64) {
        let mut slot = self.slot();
        slot.next_id += 1;
        let id = slot.next_id;
        if let Some(old) = slot.waiting.take() {
            old.task.abort();
        }
        // The grace counts from the drop, not from the task's first run.
        let deadline = tokio::time::Instant::now() + FINALIZE_GRACE;
        let this = Arc::clone(self);
        let task = tokio::spawn(async move { this.grace(ops, id, show_id, deadline).await });
        slot.waiting = Some(Waiting {
            id,
            show_id,
            task: task.abort_handle(),
        });
    }

    async fn grace<O: FinalizeOps>(
        self: Arc<Self>,
        ops: Arc<O>,
        id: u64,
        show_id: i64,
        deadline: tokio::time::Instant,
    ) {
        tokio::time::sleep_until(deadline).await;
        let live = ops.stream_active().await;
        let started = {
            let mut slot = self.slot();
            if slot.waiting.as_ref().map(|w| w.id) != Some(id) {
                return; // cancelled or replaced
            }
            if live {
                // Keep the show identity until go_live decides whether to
                // resume its recording or detach it for another producer.
                tracing::info!(
                    "Grace finalize skipped for show {}: stream is live again",
                    show_id
                );
                return;
            }
            // Dropping the wait drops the only abort handle of this task.
            slot.waiting = None;
            slot.start()
        };
        tracing::info!(
            "Grace period elapsed with no reconnect; finalizing recording for show {}",
            show_id
        );
        match self.run(&*ops, started, Some(show_id)).await {
            Ok(Some(r)) => tracing::info!(
                "Auto-finalized recording for show {} (version {}, incomplete={})",
                r.show_id,
                r.version,
                r.incomplete
            ),
            Ok(None) => {}
            Err(e) => tracing::error!("Grace finalize failed for show {}: {}", show_id, e),
        }
    }

    /// Detach after the previous finalize, then upload. Reports the detach as
    /// soon as the session is out, before the upload.
    async fn run<O: FinalizeOps>(&self, ops: &O, started: Started, show: Option<i64>) -> Outcome {
        let Started {
            id,
            after,
            detached,
            done,
        } = started;
        let _unregister = Unregister(self, id);
        if let Some(mut after) = after {
            // An error means that finalize ended without reporting; go on.
            let _ = after.wait_for(|d| *d).await;
        }
        let outcome = match ops.detach(show).await {
            Ok(Some(session)) => {
                let _ = detached.send(true);
                ops.upload(session).await.map(Some)
            }
            other => other.map(|_| None),
        };
        let _ = detached.send(true);
        let _ = done.send(Some(outcome.clone()));
        outcome
    }

    /// Start a finalize in its own task. Call with the slot lock held.
    fn spawn<O: FinalizeOps>(
        self: &Arc<Self>,
        slot: &mut Slot,
        ops: Arc<O>,
        show: Option<i64>,
    ) -> Running {
        let started = slot.start();
        let running = slot
            .running
            .last()
            .cloned()
            .expect("start registers the finalize");
        let this = Arc::clone(self);
        tokio::spawn(async move { this.run(&*ops, started, show).await });
        running
    }

    /// A producer goes live, for `show_id` or without a show (rehearsal).
    /// - A wait for the same show is cancelled, and the recording resumes.
    /// - A wait for another show becomes that show's finalize at once.
    ///
    /// Returns once every started finalize has detached its session. A
    /// finalize that started before this call runs to its end.
    pub async fn go_live<O: FinalizeOps>(self: &Arc<Self>, ops: Arc<O>, show_id: Option<i64>) {
        let newest = {
            let mut slot = self.slot();
            match slot.waiting.take() {
                Some(w) if Some(w.show_id) == show_id => {
                    w.task.abort();
                    tracing::debug!(
                        "Cancelled grace finalize for show {}: producer is back",
                        w.show_id
                    );
                }
                Some(w) => {
                    w.task.abort();
                    tracing::info!(
                        "Another producer went live; finalizing recording for show {} now",
                        w.show_id
                    );
                    self.spawn(&mut slot, ops, Some(w.show_id));
                }
                None => {}
            }
            slot.running.last().map(|r| r.detached.clone())
        };
        if let Some(mut newest) = newest {
            let _ = newest.wait_for(|d| *d).await;
        }
    }

    /// Finalize the current recording now (explicit stop, admin stop, the
    /// stop endpoint) and wait for it and for every finalize already running.
    /// A grace wait ends without its own finalize. A running finalize is never
    /// aborted: this one detaches after it, so no session is finalized twice.
    /// If this one finds no session, the newest earlier result counts.
    pub async fn finalize_now<O: FinalizeOps>(self: &Arc<Self>, ops: Arc<O>) -> Outcome {
        let (earlier, mine) = {
            let mut slot = self.slot();
            if let Some(w) = slot.waiting.take() {
                w.task.abort();
            }
            let earlier = slot.running.clone();
            (earlier, self.spawn(&mut slot, ops, None))
        };
        let mut outcome = outcome_of(mine).await;
        for running in earlier.into_iter().rev() {
            let earlier_outcome = outcome_of(running).await;
            if matches!(outcome, Ok(None)) {
                outcome = earlier_outcome;
            }
        }
        outcome
    }
}

async fn outcome_of(mut running: Running) -> Outcome {
    match running.done.wait_for(Option::is_some).await {
        Ok(outcome) => outcome.clone().unwrap_or(Ok(None)),
        Err(_) => Err("finalize task ended without a result".to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};
    use tokio::time::{advance, Instant};

    /// Recording steps with a fake session. An upload waits until its show is
    /// open, and the grace task's live check waits for the `probe` gate, so a
    /// test can stop either step half way.
    struct Fake {
        session: Mutex<Option<i64>>,
        live: AtomicBool,
        detached: Mutex<Vec<i64>>,
        uploaded: Mutex<Vec<i64>>,
        /// Shows whose upload may finish; `None` lets every upload finish.
        open: watch::Sender<Option<Vec<i64>>>,
        probe: watch::Sender<bool>,
    }

    impl Fake {
        fn new(session: Option<i64>, uploads_open: bool) -> Arc<Self> {
            Arc::new(Self {
                session: Mutex::new(session),
                live: AtomicBool::new(false),
                detached: Mutex::default(),
                uploaded: Mutex::default(),
                open: watch::Sender::new(if uploads_open { None } else { Some(vec![]) }),
                probe: watch::Sender::new(true),
            })
        }

        /// What `ensure_recording_started` does for a show with no session.
        fn record(&self, show: i64) {
            let mut session = self.session.lock().unwrap();
            assert_eq!(*session, None, "a new show must not reuse a session");
            *session = Some(show);
        }

        fn open_all(&self) {
            self.open.send_replace(None);
        }

        fn open(&self, show: i64) {
            self.open.send_modify(|open| {
                if let Some(shows) = open {
                    shows.push(show);
                }
            });
        }

        fn session(&self) -> Option<i64> {
            *self.session.lock().unwrap()
        }

        fn detached(&self) -> Vec<i64> {
            self.detached.lock().unwrap().clone()
        }

        fn uploaded(&self) -> Vec<i64> {
            self.uploaded.lock().unwrap().clone()
        }
    }

    impl FinalizeOps for Fake {
        type Session = i64;

        fn detach(&self, show: Option<i64>) -> BoxFuture<'_, Result<Option<i64>, String>> {
            let mut session = self.session.lock().unwrap();
            let taken = match (*session, show) {
                (Some(current), Some(wanted)) if current != wanted => None,
                _ => session.take(),
            };
            if let Some(show) = taken {
                self.detached.lock().unwrap().push(show);
            }
            Box::pin(async move { Ok(taken) })
        }

        fn upload(&self, show: i64) -> BoxFuture<'_, Result<FinalizedRecording, String>> {
            Box::pin(async move {
                let mut open = self.open.subscribe();
                let _ = open
                    .wait_for(|open| open.as_ref().is_none_or(|shows| shows.contains(&show)))
                    .await;
                self.uploaded.lock().unwrap().push(show);
                Ok(FinalizedRecording {
                    show_id: show,
                    version: "v".into(),
                    marker_count: 0,
                    raw_key: "raw".into(),
                    markers_key: "markers".into(),
                    incomplete: false,
                })
            })
        }

        fn stream_active(&self) -> BoxFuture<'_, bool> {
            Box::pin(async move {
                let _ = self.probe.subscribe().wait_for(|open| *open).await;
                self.live.load(Ordering::SeqCst)
            })
        }
    }

    fn finalizer() -> Arc<Finalizer> {
        Arc::default()
    }

    /// Let spawned tasks run without moving the paused clock.
    async fn settle() {
        for _ in 0..20 {
            tokio::task::yield_now().await;
        }
    }

    fn show(outcome: Outcome) -> Option<i64> {
        outcome.unwrap().map(|r| r.show_id)
    }

    #[tokio::test(start_paused = true)]
    async fn same_show_reconnect_during_wait_resumes_the_recording() {
        let (f, ops) = (finalizer(), Fake::new(Some(1), true));
        f.schedule(ops.clone(), 1);
        advance(Duration::from_secs(10)).await;
        f.go_live(ops.clone(), Some(1)).await;
        advance(FINALIZE_GRACE * 2).await;
        settle().await;
        assert!(ops.detached().is_empty());
        assert_eq!(ops.session(), Some(1));
    }

    #[tokio::test(start_paused = true)]
    async fn other_show_during_wait_detaches_the_old_session_first() {
        let (f, ops) = (finalizer(), Fake::new(Some(1), false));
        let begin = Instant::now();
        f.schedule(ops.clone(), 1);
        advance(Duration::from_secs(10)).await;
        f.go_live(ops.clone(), Some(2)).await;
        // Show 1 is out before show 2 records, long before the grace ends.
        assert_eq!(ops.detached(), vec![1]);
        assert!(begin.elapsed() < FINALIZE_GRACE);
        ops.record(2);
        ops.open_all();
        settle().await;
        assert_eq!(ops.uploaded(), vec![1]);
        advance(FINALIZE_GRACE * 2).await;
        settle().await;
        assert_eq!(ops.detached(), vec![1]);
        assert_eq!(ops.session(), Some(2));
    }

    #[tokio::test(start_paused = true)]
    async fn rehearsal_during_wait_finalizes_the_waiting_show() {
        let (f, ops) = (finalizer(), Fake::new(Some(1), true));
        f.schedule(ops.clone(), 1);
        advance(Duration::from_secs(5)).await;
        f.go_live(ops.clone(), None).await;
        assert_eq!(ops.detached(), vec![1]);
        assert_eq!(ops.session(), None);
    }

    #[tokio::test(start_paused = true)]
    async fn go_live_at_grace_expiry_before_the_switch_cancels() {
        let (f, ops) = (finalizer(), Fake::new(Some(1), true));
        ops.probe.send_replace(false);
        f.schedule(ops.clone(), 1);
        // The timer fired; the task now waits in its live check.
        advance(FINALIZE_GRACE).await;
        settle().await;
        f.go_live(ops.clone(), Some(1)).await;
        ops.probe.send_replace(true);
        settle().await;
        assert!(ops.detached().is_empty());
        assert_eq!(ops.session(), Some(1));
    }

    #[tokio::test(start_paused = true)]
    async fn go_live_after_the_switch_waits_and_never_aborts() {
        for next in [1, 2] {
            let (f, ops) = (finalizer(), Fake::new(Some(1), false));
            f.schedule(ops.clone(), 1);
            advance(FINALIZE_GRACE).await;
            settle().await;
            // The grace finalize took the session and is uploading.
            assert_eq!(ops.detached(), vec![1]);
            f.go_live(ops.clone(), Some(next)).await;
            ops.record(next);
            ops.open_all();
            settle().await;
            assert_eq!(ops.uploaded(), vec![1], "show {next}");
            assert_eq!(ops.session(), Some(next));
        }
    }

    #[tokio::test(start_paused = true)]
    async fn stop_during_wait_finalizes_once() {
        let (f, ops) = (finalizer(), Fake::new(Some(1), true));
        f.schedule(ops.clone(), 1);
        advance(Duration::from_secs(10)).await;
        assert_eq!(show(f.finalize_now(ops.clone()).await), Some(1));
        advance(FINALIZE_GRACE * 2).await;
        settle().await;
        assert_eq!(ops.detached(), vec![1]);
        assert_eq!(ops.uploaded(), vec![1]);
    }

    #[tokio::test(start_paused = true)]
    async fn stop_at_grace_expiry_before_the_switch_finalizes_once() {
        let (f, ops) = (finalizer(), Fake::new(Some(1), true));
        ops.probe.send_replace(false);
        f.schedule(ops.clone(), 1);
        advance(FINALIZE_GRACE).await;
        settle().await;
        assert_eq!(show(f.finalize_now(ops.clone()).await), Some(1));
        ops.probe.send_replace(true);
        settle().await;
        assert_eq!(ops.detached(), vec![1]);
        assert_eq!(ops.uploaded(), vec![1]);
    }

    #[tokio::test(start_paused = true)]
    async fn stop_during_upload_waits_for_it() {
        let (f, ops) = (finalizer(), Fake::new(Some(1), false));
        f.schedule(ops.clone(), 1);
        advance(FINALIZE_GRACE).await;
        settle().await;
        let stop = tokio::spawn({
            let (f, ops) = (f.clone(), ops.clone());
            async move { f.finalize_now(ops).await }
        });
        settle().await;
        assert!(!stop.is_finished(), "stop must wait for the running upload");
        ops.open_all();
        assert_eq!(show(stop.await.unwrap()), Some(1));
        assert_eq!(ops.detached(), vec![1]);
        assert_eq!(ops.uploaded(), vec![1]);
    }

    #[tokio::test(start_paused = true)]
    async fn stop_with_its_own_session_still_waits_for_an_older_finalize() {
        let (f, ops) = (finalizer(), Fake::new(Some(1), false));
        f.schedule(ops.clone(), 1);
        advance(FINALIZE_GRACE).await;
        settle().await;
        f.go_live(ops.clone(), Some(2)).await;
        ops.record(2);
        let stop = tokio::spawn({
            let (f, ops) = (f.clone(), ops.clone());
            async move { f.finalize_now(ops).await }
        });
        ops.open(2);
        settle().await;
        assert_eq!(ops.uploaded(), vec![2]);
        assert!(!stop.is_finished(), "stop must wait for show 1's upload");
        ops.open(1);
        assert_eq!(show(stop.await.unwrap()), Some(2));
        assert_eq!(ops.uploaded(), vec![2, 1]);
    }

    #[tokio::test(start_paused = true)]
    async fn stop_after_reconnect_finalizes_the_new_session_too() {
        let (f, ops) = (finalizer(), Fake::new(Some(1), false));
        f.schedule(ops.clone(), 1);
        advance(FINALIZE_GRACE).await;
        settle().await;
        f.go_live(ops.clone(), Some(1)).await;
        ops.record(1);
        let stop = tokio::spawn({
            let (f, ops) = (f.clone(), ops.clone());
            async move { f.finalize_now(ops).await }
        });
        settle().await;
        ops.open_all();
        assert_eq!(show(stop.await.unwrap()), Some(1));
        assert_eq!(ops.detached(), vec![1, 1]);
        assert_eq!(ops.uploaded().len(), 2);
    }

    #[tokio::test(start_paused = true)]
    async fn new_grace_wait_keeps_an_older_finalize_running() {
        let (f, ops) = (finalizer(), Fake::new(Some(1), false));
        f.schedule(ops.clone(), 1);
        advance(FINALIZE_GRACE).await;
        settle().await;
        f.go_live(ops.clone(), Some(2)).await;
        ops.record(2);
        f.schedule(ops.clone(), 2);
        advance(Duration::from_secs(10)).await;
        ops.open_all();
        settle().await;
        assert_eq!(ops.uploaded(), vec![1]);
        advance(FINALIZE_GRACE).await;
        settle().await;
        assert_eq!(ops.detached(), vec![1, 2]);
        assert_eq!(ops.uploaded(), vec![1, 2]);
    }

    #[tokio::test(start_paused = true)]
    async fn new_grace_wait_replaces_an_older_wait() {
        let (f, ops) = (finalizer(), Fake::new(Some(1), true));
        f.schedule(ops.clone(), 1);
        advance(Duration::from_secs(20)).await;
        f.schedule(ops.clone(), 1);
        advance(Duration::from_secs(20)).await;
        settle().await;
        assert!(ops.detached().is_empty());
        advance(Duration::from_secs(15)).await;
        settle().await;
        assert_eq!(ops.detached(), vec![1]);
    }

    #[tokio::test(start_paused = true)]
    async fn same_show_after_live_grace_probe_resumes_the_recording() {
        let (f, ops) = (finalizer(), Fake::new(Some(1), true));
        ops.probe.send_replace(false);
        f.schedule(ops.clone(), 1);
        advance(FINALIZE_GRACE).await;
        settle().await;
        ops.live.store(true, Ordering::SeqCst);
        ops.probe.send_replace(true);
        settle().await;
        assert!(ops.detached().is_empty());
        assert_eq!(f.slot().waiting.as_ref().map(|w| w.show_id), Some(1));

        f.go_live(ops.clone(), Some(1)).await;
        assert!(f.slot().waiting.is_none());
        assert_eq!(ops.session(), Some(1));
        assert!(ops.uploaded().is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn other_show_after_live_grace_probe_detaches_the_old_session_first() {
        let (f, ops) = (finalizer(), Fake::new(Some(1), false));
        ops.probe.send_replace(false);
        f.schedule(ops.clone(), 1);
        advance(FINALIZE_GRACE).await;
        settle().await;
        ops.live.store(true, Ordering::SeqCst);
        ops.probe.send_replace(true);
        settle().await;

        f.go_live(ops.clone(), Some(2)).await;
        assert_eq!(ops.detached(), vec![1]);
        ops.record(2);
        ops.open_all();
        settle().await;
        assert_eq!(ops.uploaded(), vec![1]);
        assert_eq!(ops.session(), Some(2));
    }

    #[tokio::test(start_paused = true)]
    async fn rehearsal_after_live_grace_probe_finalizes_the_waiting_show() {
        let (f, ops) = (finalizer(), Fake::new(Some(1), false));
        ops.probe.send_replace(false);
        f.schedule(ops.clone(), 1);
        advance(FINALIZE_GRACE).await;
        settle().await;
        ops.live.store(true, Ordering::SeqCst);
        ops.probe.send_replace(true);
        settle().await;

        f.go_live(ops.clone(), None).await;
        assert_eq!(ops.detached(), vec![1]);
        assert_eq!(ops.session(), None);
        ops.open_all();
        settle().await;
        assert_eq!(ops.uploaded(), vec![1]);
    }
}
