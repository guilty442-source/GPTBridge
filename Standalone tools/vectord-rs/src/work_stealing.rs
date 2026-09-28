use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TryRecvError, TrySendError};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

const IDLE_WAIT: Duration = Duration::from_millis(50);
const LOCAL_DRAIN_LIMIT: usize = 4;

struct Task {
    cancel: Arc<AtomicBool>,
    run: Option<Box<dyn FnOnce() + Send + 'static>>,
}

#[derive(Clone)]
pub struct TaskHandle {
    cancel: Arc<AtomicBool>,
}

impl TaskHandle {
    pub fn cancel(&self) {
        self.cancel.store(true, Ordering::Release);
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct PoolMetrics {
    pub submitted: usize,
    pub completed: usize,
    pub cancelled: usize,
    pub rejected: usize,
    pub pending: usize,
    pub running: usize,
}

struct Metrics {
    submitted: AtomicUsize,
    completed: AtomicUsize,
    cancelled: AtomicUsize,
    rejected: AtomicUsize,
    pending: AtomicUsize,
    running: AtomicUsize,
}

impl Metrics {
    fn snapshot(&self) -> PoolMetrics {
        PoolMetrics {
            submitted: self.submitted.load(Ordering::Relaxed),
            completed: self.completed.load(Ordering::Relaxed),
            cancelled: self.cancelled.load(Ordering::Relaxed),
            rejected: self.rejected.load(Ordering::Relaxed),
            pending: self.pending.load(Ordering::Relaxed),
            running: self.running.load(Ordering::Relaxed),
        }
    }
}

pub struct WorkStealingPool {
    sender: Mutex<Option<SyncSender<Task>>>,
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
        let metrics = Arc::new(Metrics {
            submitted: AtomicUsize::new(0),
            completed: AtomicUsize::new(0),
            cancelled: AtomicUsize::new(0),
            rejected: AtomicUsize::new(0),
            pending: AtomicUsize::new(0),
            running: AtomicUsize::new(0),
        });
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
            stop,
            queues,
            metrics,
            workers: Mutex::new(handles),
        }
    }

    pub fn submit<F>(&self, task: F) -> Result<TaskHandle, ()>
    where
        F: FnOnce() + Send + 'static,
    {
        let cancel = Arc::new(AtomicBool::new(false));
        let handle = TaskHandle { cancel: cancel.clone() };
        let item = Task { cancel, run: Some(Box::new(task)) };
        let sender = self.sender.lock().map_err(|_| ())?;
        let Some(sender) = sender.as_ref() else {
            self.metrics.rejected.fetch_add(1, Ordering::Relaxed);
            return Err(());
        };
        match sender.try_send(item) {
            Ok(()) => {
                self.metrics.submitted.fetch_add(1, Ordering::Relaxed);
                self.metrics.pending.fetch_add(1, Ordering::Relaxed);
                Ok(handle)
            }
            Err(TrySendError::Full(_)) | Err(TrySendError::Disconnected(_)) => {
                self.metrics.rejected.fetch_add(1, Ordering::Relaxed);
                Err(())
            }
        }
    }

    pub fn metrics(&self) -> PoolMetrics {
        self.metrics.snapshot()
    }

    pub fn shutdown(&self) {
        self.stop.store(true, Ordering::Release);
        self.sender.lock().ok().and_then(|mut sender| sender.take());
        if let Ok(mut workers) = self.workers.lock() {
            for worker in workers.drain(..) {
                let _ = worker.join();
            }
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
    use super::WorkStealingPool;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    #[test]
    fn bounded_pool_executes_and_reports_metrics() {
        let pool = WorkStealingPool::new(2, 8);
        let completed = Arc::new(AtomicUsize::new(0));
        for _ in 0..4 {
            let completed = completed.clone();
            assert!(pool.submit(move || {
                completed.fetch_add(1, Ordering::Relaxed);
            }).is_ok());
        }
        for _ in 0..100 {
            if completed.load(Ordering::Relaxed) == 4 {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
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
            std::thread::sleep(std::time::Duration::from_millis(40));
        }).unwrap();
        while gate.load(Ordering::Acquire) == 0 {
            std::thread::yield_now();
        }
        let ran_clone = ran.clone();
        let handle = pool.submit(move || {
            ran_clone.fetch_add(1, Ordering::Relaxed);
        }).unwrap();
        handle.cancel();
        std::thread::sleep(std::time::Duration::from_millis(60));
        assert_eq!(ran.load(Ordering::Relaxed), 0);
        assert!(pool.metrics().cancelled >= 1);
        pool.shutdown();
    }
}
