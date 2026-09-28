//! bounded-concurrency/v1 work pool: bounded admission queue + fixed
//! worker pool + per-worker local deques with stealing.
//!
//! Queue contract (six properties, B16/B157):
//! - capacity: bounded global channel + caller-declared worker count
//! - priority: tasks carry a rank; workers run higher ranks first,
//!   steal takes the victim's lowest rank
//! - deadline: queued tasks expire instead of running stale work
//! - backpressure: submit may wait a bounded budget when full
//! - cancellation: TaskHandle flag is honoured before execution
//! - drop/reject: full queue + zero wait rejects; shutdown drains
//!   survivors and counts them dropped

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TryRecvError, TrySendError};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const IDLE_WAIT: Duration = Duration::from_millis(50);
const LOCAL_DRAIN_LIMIT: usize = 4;

/// Admission options; `priority` ranks lower-is-sooner so the default
/// keeps plain FIFO for callers that do not differentiate work.
#[allow(dead_code)] // contract surface: consumers may set any subset
#[derive(Clone, Copy, Debug)]
pub struct TaskOpts {
    pub priority: u8,
    pub deadline: Option<Duration>,
    pub wait: Duration,
}

impl Default for TaskOpts {
    fn default() -> Self {
        Self { priority: u8::MAX, deadline: None, wait: Duration::ZERO }
    }
}

struct Task {
    cancel: Arc<AtomicBool>,
    run: Option<Box<dyn FnOnce() + Send + 'static>>,
    priority: u8,
    enqueued: Instant,
    deadline: Option<Duration>,
}

impl Task {
    fn expired(&self) -> bool {
        self.deadline
            .map(|d| self.enqueued.elapsed() >= d)
            .unwrap_or(false)
    }
}

#[derive(Clone)]
pub struct TaskHandle {
    cancel: Arc<AtomicBool>,
}

#[allow(dead_code)] // cancel() is the contract surface; callers may drop handles
impl TaskHandle {
    pub fn cancel(&self) {
        self.cancel.store(true, Ordering::Release);
    }
}

#[allow(dead_code)] // observability surface; populated by every pool
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct PoolMetrics {
    pub submitted: usize,
    pub completed: usize,
    pub cancelled: usize,
    pub rejected: usize,
    pub expired: usize,
    pub dropped: usize,
    pub pending: usize,
    pub running: usize,
}

struct Metrics {
    submitted: AtomicUsize,
    completed: AtomicUsize,
    cancelled: AtomicUsize,
    rejected: AtomicUsize,
    expired: AtomicUsize,
    dropped: AtomicUsize,
    pending: AtomicUsize,
    running: AtomicUsize,
}

impl Metrics {
    fn new() -> Self {
        Self {
            submitted: AtomicUsize::new(0),
            completed: AtomicUsize::new(0),
            cancelled: AtomicUsize::new(0),
            rejected: AtomicUsize::new(0),
            expired: AtomicUsize::new(0),
            dropped: AtomicUsize::new(0),
            pending: AtomicUsize::new(0),
            running: AtomicUsize::new(0),
        }
    }

    fn snapshot(&self) -> PoolMetrics {
        PoolMetrics {
            submitted: self.submitted.load(Ordering::Relaxed),
            completed: self.completed.load(Ordering::Relaxed),
            cancelled: self.cancelled.load(Ordering::Relaxed),
            rejected: self.rejected.load(Ordering::Relaxed),
            expired: self.expired.load(Ordering::Relaxed),
            dropped: self.dropped.load(Ordering::Relaxed),
            pending: self.pending.load(Ordering::Relaxed),
            running: self.running.load(Ordering::Relaxed),
        }
    }
}

pub struct WorkStealingPool {
    sender: Mutex<Option<SyncSender<Task>>>,
    queues: Arc<Vec<Mutex<VecDeque<Task>>>>,
    stop: Arc<AtomicBool>,
    metrics: Arc<Metrics>,
    workers: Mutex<Vec<JoinHandle<()>>>,
}

impl WorkStealingPool {
    pub fn new(worker_count: usize, capacity: usize) -> Self {
        assert!(worker_count > 0, "worker_count must be positive");
        assert!(capacity > 0, "capacity must be positive");
        let (sender, receiver) = sync_channel(capacity);
        let queues = Arc::new(
            (0..worker_count)
                .map(|_| Mutex::new(VecDeque::new()))
                .collect::<Vec<_>>(),
        );
        let stop = Arc::new(AtomicBool::new(false));
        let metrics = Arc::new(Metrics::new());
        let shared_receiver = Arc::new(Mutex::new(receiver));
        let mut handles = Vec::with_capacity(worker_count);
        for id in 0..worker_count {
            handles.push(Self::spawn_worker(
                id,
                shared_receiver.clone(),
                queues.clone(),
                stop.clone(),
                metrics.clone(),
            ));
        }
        Self {
            sender: Mutex::new(Some(sender)),
            queues,
            stop,
            metrics,
            workers: Mutex::new(handles),
        }
    }

    /// Immediate admit-or-reject (`wait=0`, normal priority, no expiry).
    #[allow(dead_code)] // contract surface: submit_opts covers callers today
    pub fn submit<F>(&self, task: F) -> Result<TaskHandle, ()>
    where
        F: FnOnce() + Send + 'static,
    {
        self.submit_opts(task, TaskOpts::default())
    }

    /// Bounded admission: `opts.wait` is the backpressure budget spent
    /// retrying a full queue before rejecting; `opts.deadline` expires
    /// the task if it is still queued when the budget elapses.
    pub fn submit_opts<F>(&self, task: F, opts: TaskOpts) -> Result<TaskHandle, ()>
    where
        F: FnOnce() + Send + 'static,
    {
        let cancel = Arc::new(AtomicBool::new(false));
        let handle = TaskHandle {
            cancel: cancel.clone(),
        };
        let item = Task {
            cancel,
            run: Some(Box::new(task)),
            priority: opts.priority,
            enqueued: Instant::now(),
            deadline: opts.deadline,
        };
        /* bounded backpressure: retry try_send until the wait budget
           is spent; the sender lock is held for one attempt only so
           other submitters keep flowing between retries. */
        let mut item = item;
        let wait_deadline = Instant::now() + opts.wait;
        loop {
            match self.try_admit(item) {
                Ok(()) => {
                    self.metrics.submitted.fetch_add(1, Ordering::Relaxed);
                    self.metrics.pending.fetch_add(1, Ordering::Relaxed);
                    return Ok(handle);
                }
                Err(TrySendError::Disconnected(_)) => {
                    self.metrics.rejected.fetch_add(1, Ordering::Relaxed);
                    return Err(());
                }
                Err(TrySendError::Full(returned)) => {
                    if opts.wait.is_zero() || Instant::now() >= wait_deadline {
                        self.metrics.rejected.fetch_add(1, Ordering::Relaxed);
                        return Err(());
                    }
                    item = returned;
                    thread::sleep(Duration::from_millis(2).min(opts.wait));
                }
            }
        }
    }

    fn try_admit(&self, item: Task) -> Result<(), TrySendError<Task>> {
        let guard = match self.sender.lock() {
            Ok(g) => g,
            Err(_) => return Err(TrySendError::Disconnected(item)),
        };
        match guard.as_ref() {
            Some(sender) => sender.try_send(item),
            None => Err(TrySendError::Disconnected(item)),
        }
    }

    #[allow(dead_code)] // observability surface for future /metrics wiring
    pub fn metrics(&self) -> PoolMetrics {
        self.metrics.snapshot()
    }

    /// Stop intake, let running tasks finish, then count anything still
    /// queued as dropped (reject/drop policy on shutdown).
    pub fn shutdown(&self) {
        self.stop.store(true, Ordering::Release);
        self.sender.lock().ok().and_then(|mut sender| sender.take());
        if let Ok(mut workers) = self.workers.lock() {
            for worker in workers.drain(..) {
                let _ = worker.join();
            }
        }
        let mut dropped = 0usize;
        for queue in self.queues.iter() {
            if let Ok(mut q) = queue.lock() {
                dropped += q.drain(..).count();
            }
        }
        if dropped > 0 {
            self.metrics.dropped.fetch_add(dropped, Ordering::Relaxed);
            self.metrics.pending.fetch_sub(dropped, Ordering::Relaxed);
        }
    }

    fn spawn_worker(
        id: usize,
        receiver: Arc<Mutex<Receiver<Task>>>,
        queues: Arc<Vec<Mutex<VecDeque<Task>>>>,
        stop: Arc<AtomicBool>,
        metrics: Arc<Metrics>,
    ) -> JoinHandle<()> {
        thread::Builder::new()
            .name(format!("vectord-ws-{id}"))
            .spawn(move || {
                while !stop.load(Ordering::Acquire) {
                    let task = Self::take_local(&queues[id])
                        .or_else(|| Self::steal(id, &queues))
                        .or_else(|| Self::receive_global(&receiver, &queues[id]));
                    let Some(mut task) = task else {
                        continue;
                    };
                    metrics.pending.fetch_sub(1, Ordering::Relaxed);
                    if task.expired() {
                        metrics.expired.fetch_add(1, Ordering::Relaxed);
                        continue;
                    }
                    if task.cancel.load(Ordering::Acquire) {
                        metrics.cancelled.fetch_add(1, Ordering::Relaxed);
                        continue;
                    }
                    metrics.running.fetch_add(1, Ordering::Relaxed);
                    if let Some(run) = task.run.take() {
                        run();
                    }
                    metrics.running.fetch_sub(1, Ordering::Relaxed);
                    metrics.completed.fetch_add(1, Ordering::Relaxed);
                }
            })
            .expect("vectord work-stealing worker spawn")
    }

    fn take_local(queue: &Mutex<VecDeque<Task>>) -> Option<Task> {
        queue.lock().ok()?.pop_front()
    }

    fn steal(id: usize, queues: &[Mutex<VecDeque<Task>>]) -> Option<Task> {
        for offset in 1..queues.len() {
            let victim = (id + offset) % queues.len();
            if let Ok(mut queue) = queues[victim].try_lock() {
                /* pop_back takes the victim's lowest-rank task. */
                if let Some(task) = queue.pop_back() {
                    return Some(task);
                }
            }
        }
        None
    }

    fn receive_global(
        receiver: &Mutex<Receiver<Task>>,
        local: &Mutex<VecDeque<Task>>,
    ) -> Option<Task> {
        let first = receiver.lock().ok()?.recv_timeout(IDLE_WAIT).ok()?;
        let mut queue = local.lock().ok()?;
        queue.push_back(first);
        if let Ok(shared) = receiver.try_lock() {
            for _ in 1..LOCAL_DRAIN_LIMIT {
                match shared.try_recv() {
                    Ok(task) => queue.push_back(task),
                    Err(TryRecvError::Empty | TryRecvError::Disconnected) => break,
                }
            }
        }
        /* stable rank sort: FIFO is preserved inside one priority. */
        queue.make_contiguous().sort_by_key(|t| t.priority);
        queue.pop_front()
    }
}

impl Drop for WorkStealingPool {
    fn drop(&mut self) {
        self.shutdown();
    }
}

#[cfg(test)]
mod tests {
    use super::{TaskOpts, WorkStealingPool};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    fn wait_until(pred: impl Fn() -> bool) {
        for _ in 0..200 {
            if pred() {
                return;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    }

    #[test]
    fn bounded_pool_executes_and_reports_metrics() {
        let pool = WorkStealingPool::new(2, 8);
        let completed = Arc::new(AtomicUsize::new(0));
        for _ in 0..4 {
            let completed = completed.clone();
            assert!(pool
                .submit(move || {
                    completed.fetch_add(1, Ordering::Relaxed);
                })
                .is_ok());
        }
        wait_until(|| completed.load(Ordering::Relaxed) == 4);
        assert_eq!(completed.load(Ordering::Relaxed), 4);
        assert_eq!(pool.metrics().completed, 4);
        pool.shutdown();
    }

    #[test]
    fn cancelled_queued_task_does_not_run() {
        let pool = WorkStealingPool::new(1, 4);
        let ran = Arc::new(AtomicUsize::new(0));
        let gate = Arc::new(AtomicUsize::new(0));
        let first_gate = gate.clone();
        pool.submit(move || {
            first_gate.store(1, Ordering::Release);
            std::thread::sleep(Duration::from_millis(40));
        })
        .unwrap();
        wait_until(|| gate.load(Ordering::Acquire) == 1);
        let ran_clone = ran.clone();
        let handle = pool
            .submit(move || {
                ran_clone.fetch_add(1, Ordering::Relaxed);
            })
            .unwrap();
        handle.cancel();
        wait_until(|| pool.metrics().cancelled >= 1);
        assert_eq!(ran.load(Ordering::Relaxed), 0);
        pool.shutdown();
    }

    #[test]
    fn higher_priority_runs_before_queued_normal() {
        let pool = WorkStealingPool::new(1, 8);
        let gate = Arc::new(AtomicUsize::new(0));
        let order = Arc::new(Mutex::new(Vec::new()));
        let g = gate.clone();
        pool.submit(move || {
            g.store(1, Ordering::Release);
            std::thread::sleep(Duration::from_millis(60));
        })
        .unwrap();
        wait_until(|| gate.load(Ordering::Acquire) == 1);
        for (tag, prio) in [("lo", 200u8), ("hi", 10u8)] {
            let order = order.clone();
            pool.submit_opts(
                move || order.lock().unwrap().push(tag),
                TaskOpts { priority: prio, ..TaskOpts::default() },
            )
            .unwrap();
        }
        wait_until(|| order.lock().unwrap().len() == 2);
        assert_eq!(*order.lock().unwrap(), vec!["hi", "lo"]);
        pool.shutdown();
    }

    #[test]
    fn queued_task_past_deadline_expires() {
        let pool = WorkStealingPool::new(1, 8);
        let gate = Arc::new(AtomicUsize::new(0));
        let g = gate.clone();
        pool.submit(move || {
            g.store(1, Ordering::Release);
            std::thread::sleep(Duration::from_millis(120));
        })
        .unwrap();
        wait_until(|| gate.load(Ordering::Acquire) == 1);
        pool.submit_opts(
            || {},
            TaskOpts { deadline: Some(Duration::from_millis(10)), ..TaskOpts::default() },
        )
        .unwrap();
        wait_until(|| pool.metrics().expired >= 1);
        assert!(pool.metrics().expired >= 1);
        pool.shutdown();
    }

    #[test]
    fn backpressure_wait_blocks_then_rejects() {
        let pool = WorkStealingPool::new(1, 1);
        let gate = Arc::new(AtomicUsize::new(0));
        let g = gate.clone();
        pool.submit(move || {
            g.store(1, Ordering::Release);
            std::thread::sleep(Duration::from_millis(150));
        })
        .unwrap();
        wait_until(|| gate.load(Ordering::Acquire) == 1);
        pool.submit(|| {}).unwrap(); // fills capacity 1
        let blocked = pool.submit_opts(
            || {},
            TaskOpts { wait: Duration::from_millis(80), ..TaskOpts::default() },
        );
        assert!(blocked.is_err());
        assert!(pool.metrics().rejected >= 1);
        pool.shutdown();
    }
}
