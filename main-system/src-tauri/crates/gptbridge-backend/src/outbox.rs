//! outbox.rs — A195 transactional outbox over the native WS gateway.
//!
//! Port of ``tasks/state_outbox.py`` + the read path of
//! ``tasks/state_outbox_store.py`` (PgOutboxStore).  The authoritative store
//! is ``gptbridge_transport.outbox_event`` / ``outbox_entity_revision``
//! (migration 147); PostgreSQL stays the sole structured-data authority.
//!
//! Contract parity:
//!   * ``state_event_hello``  → ``state_event_session`` reply (cursor, reset,
//!     backend_generation, release_id, contract_version, latest_sequence);
//!     a superseded/missing client generation starts at the latest sequence
//!     instead of replaying full durable history.
//!   * ``state_event_ack``    → silent; the acknowledged cursor only moves up.
//!   * ``state_event_resync`` → ``state_event_resync_result`` + replay from
//!     the supplied cursor.
//!   * Delivery: at-least-once, bounded window (acked+200), drain batch 100,
//!     unacked batches re-sent after 2 s, poll wake 0.5 s — events leave as
//!     ``state_event`` frames carrying ``idempotency_key`` =
//!     ``<backend_generation>:<sequence>``.
//!   * BACKPRESSURE: a session cannot run ahead of its acked cursor + window.
//!   * PG unavailable → no hub (fail-closed, parity with the retired Python
//!     publisher that simply was never constructed).

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use gptbridge_core::app::PRODUCT_VERSION;
use gptbridge_core::ipc::ws_server::ServerWriter;

use crate::pg;

const OUTBOX_EVENT_NAME: &str = "state_event";
const CONTRACT_VERSION: &str = "a195.state-event.v1";
const DELIVERY_WINDOW: i64 = 200;
const RETRY_INTERVAL: Duration = Duration::from_secs(2);
const POLL_INTERVAL: Duration = Duration::from_millis(500);
const DRAIN_BATCH_LIMIT: i64 = 100;
const RETENTION_MIN_EVENTS: i64 = 5000;
const PRUNE_MIN_INTERVAL: Duration = Duration::from_secs(5);

struct Session {
    session_id: String,
    writer: ServerWriter,
    acked: i64,
    sent_upto: i64,
    last_attempt: Instant,
}

pub struct OutboxHub {
    backend_generation: String,
    sessions: Mutex<HashMap<u64, Session>>,
    shutdown: AtomicBool,
}

/// Connect + verify the outbox tables are readable.  ``None`` when the
/// governed DSN is absent or PostgreSQL is down — the caller then leaves the
/// outbox silent (identical to the Python startup path).
pub fn try_new() -> Option<Arc<OutboxHub>> {
    let mut client = pg::connect()?;
    client
        .query(
            "SELECT COALESCE(MAX(sequence), 0) FROM gptbridge_transport.outbox_event",
            &[],
        )
        .ok()?;
    let hub = Arc::new(OutboxHub {
        backend_generation: uuid::Uuid::new_v4().simple().to_string(),
        sessions: Mutex::new(HashMap::new()),
        shutdown: AtomicBool::new(false),
    });
    let worker = Arc::clone(&hub);
    thread::spawn(move || worker.drain_loop(client));
    Some(hub)
}

impl OutboxHub {
    fn ensure_session(&self, conn: u64, writer: &ServerWriter) -> String {
        let mut sessions = self.sessions.lock().unwrap();
        let entry = sessions.entry(conn).or_insert_with(|| {
            Session {
                session_id: uuid::Uuid::new_v4().simple().to_string(),
                writer: writer.clone(),
                acked: 0,
                sent_upto: 0,
                last_attempt: Instant::now()
                    .checked_sub(RETRY_INTERVAL)
                    .unwrap_or_else(Instant::now),
            }
        });
        entry.session_id.clone()
    }

    /// ``state_event_hello`` — register/resubscribe, answer the session
    /// descriptor.  Fresh or stale-generation clients reset to latest.
    pub fn handle_hello(
        &self,
        conn: u64,
        writer: &ServerWriter,
        cursor: &Value,
        generation: &Value,
    ) -> Value {
        let session_id = self.ensure_session(conn, writer);
        let mut cursor_int = cursor.as_i64().unwrap_or(0).max(0);
        let client_generation = generation.as_str().unwrap_or("").trim();
        let latest_sequence = self.latest_sequence();
        let reset = client_generation != self.backend_generation;
        if reset {
            cursor_int = latest_sequence;
        }
        let mut sessions = self.sessions.lock().unwrap();
        if let Some(session) = sessions.get_mut(&conn) {
            session.acked = cursor_int;
            session.sent_upto = cursor_int;
            session.last_attempt = Instant::now()
                .checked_sub(RETRY_INTERVAL)
                .unwrap_or_else(Instant::now);
        }
        json!({
            "session_id": session_id,
            "backend_generation": self.backend_generation,
            "release_id": PRODUCT_VERSION,
            "contract_version": CONTRACT_VERSION,
            "cursor": cursor_int,
            "reset": reset,
            "latest_sequence": latest_sequence,
        })
    }

    /// ``state_event_ack`` — the acknowledged cursor never moves backwards.
    pub fn handle_ack(&self, conn: u64, cursor: &Value) {
        let Some(ack) = cursor.as_i64() else {
            return;
        };
        let mut sessions = self.sessions.lock().unwrap();
        if let Some(session) = sessions.get_mut(&conn) {
            if ack > session.acked {
                session.acked = ack;
            }
        }
    }

    /// ``state_event_resync`` — reset the session cursor; delivery replays
    /// from it on the next drain.  Unknown connections degrade to hello(0).
    pub fn handle_resync(&self, conn: u64, cursor: &Value) -> Option<Value> {
        let cursor_int = cursor.as_i64().unwrap_or(0).max(0);
        let mut sessions = self.sessions.lock().unwrap();
        let session = sessions.get_mut(&conn)?;
        session.acked = cursor_int;
        session.sent_upto = cursor_int;
        session.last_attempt = Instant::now()
            .checked_sub(RETRY_INTERVAL)
            .unwrap_or_else(Instant::now);
        Some(json!({
            "session_id": session.session_id,
            "cursor": session.acked,
            "resync": true,
        }))
    }

    pub fn unregister(&self, conn: u64) {
        self.sessions.lock().unwrap().remove(&conn);
    }

    #[allow(dead_code)]
    pub fn shutdown(&self) {
        self.shutdown.store(true, Ordering::SeqCst);
    }

    fn latest_sequence(&self) -> i64 {
        pg::connect()
            .and_then(|mut c| {
                c.query(
                    "SELECT COALESCE(MAX(sequence), 0) FROM gptbridge_transport.outbox_event",
                    &[],
                )
                .ok()
            })
            .and_then(|rows| rows.first().map(|r| r.get::<_, i64>(0)))
            .unwrap_or(0)
    }

    fn fetch_after(
        client: &mut postgres::Client,
        generation: &str,
        sequence: i64,
        limit: i64,
    ) -> Vec<Value> {
        let Ok(rows) = client.query(
            "SELECT sequence, entity_id, entity_type, operation, \
             authoritative_revision, previous_revision, \
             changed_field_allowlist::text, invalidation_keys::text, \
             state_hash, backend_generation, release_id, contract_version, \
             correlation_id, committed_at::text \
             FROM gptbridge_transport.outbox_event WHERE sequence > $1 \
             ORDER BY sequence ASC LIMIT $2",
            &[&sequence, &limit],
        ) else {
            return Vec::new();
        };
        rows.iter()
            .map(|r| {
                let sequence: i64 = r.get(0);
                json!({
                    "sequence": sequence,
                    "entity_id": r.get::<_, String>(1),
                    "entity_type": r.get::<_, String>(2),
                    "operation": r.get::<_, String>(3),
                    "authoritative_revision": r.get::<_, i64>(4),
                    "previous_revision": r.get::<_, i64>(5),
                    "changed_field_allowlist": serde_json::from_str::<Value>(
                        &r.get::<_, String>(6)).unwrap_or(json!([])),
                    "invalidation_keys": serde_json::from_str::<Value>(
                        &r.get::<_, String>(7)).unwrap_or(json!([])),
                    "state_hash": r.get::<_, String>(8),
                    "backend_generation": r.get::<_, String>(9),
                    "release_id": r.get::<_, String>(10),
                    "contract_version": r.get::<_, String>(11),
                    "correlation_id": r.get::<_, String>(12),
                    "committed_at": r.get::<_, String>(13),
                    "idempotency_key": format!("{generation}:{sequence}"),
                })
            })
            .collect()
    }

    /// Poll → deliver the bounded window to every live session → prune.
    /// Each tick opens with one cheap ``MAX(sequence)`` probe on the
    /// shared connection — the wide ``fetch_after`` SELECT then runs
    /// only for sessions actually behind the latest sequence, so idle
    /// sessions cost a single indexed row-read per poll instead of a
    /// full event fetch each.
    fn drain_loop(&self, mut client: postgres::Client) {
        let mut last_prune = Instant::now();
        while !self.shutdown.load(Ordering::SeqCst) {
            // Idle fast path: reap sessions whose writer died, and when
            // no subscriber remains skip the PostgreSQL probe outright —
            // an unattached backend must not issue outbox queries at all
            // (was: one MAX(sequence) every 500 ms forever).  Events
            // accumulate server-side until the next state_event_hello.
            if {
                let mut sessions = self.sessions.lock().unwrap();
                sessions.retain(|_, s| !s.writer.is_closed());
                sessions.is_empty()
            } {
                thread::sleep(POLL_INTERVAL);
                continue;
            }
            let latest = Self::max_sequence(&mut client);
            let deliveries: Vec<(u64, ServerWriter, i64, i64)> = {
                let sessions = self.sessions.lock().unwrap();
                sessions
                    .iter()
                    .filter(|(_, s)| s.last_attempt.elapsed() >= RETRY_INTERVAL
                        && s.sent_upto < s.acked + DELIVERY_WINDOW
                        && s.sent_upto < latest)
                    .map(|(k, s)| (*k, s.writer.clone(), s.acked, s.sent_upto))
                    .collect()
            };
            for (conn, writer, acked, sent_upto) in deliveries {
                if writer.is_closed() {
                    continue;
                }
                let limit = (DRAIN_BATCH_LIMIT)
                    .min(acked + DELIVERY_WINDOW - sent_upto);
                let events = Self::fetch_after(
                    &mut client, &self.backend_generation, sent_upto, limit);
                if events.is_empty() {
                    continue;
                }
                let mut delivered = sent_upto;
                for event in &events {
                    if !writer.send(&json!({
                        "event": OUTBOX_EVENT_NAME,
                        "payload": event,
                    })) {
                        break;
                    }
                    delivered = event["sequence"].as_i64().unwrap_or(delivered);
                }
                let mut sessions = self.sessions.lock().unwrap();
                if let Some(session) = sessions.get_mut(&conn) {
                    session.sent_upto = delivered.max(session.sent_upto);
                    session.last_attempt = Instant::now();
                }
            }
            if last_prune.elapsed() >= PRUNE_MIN_INTERVAL {
                last_prune = Instant::now();
                if latest >= 0 {
                    self.prune(&mut client, latest);
                }
            }
            thread::sleep(POLL_INTERVAL);
        }
    }

    /// Latest committed sequence; ``-1`` when PostgreSQL is unreachable —
    /// callers then skip delivery and pruning for that tick (the fetch
    /// path already degrades to empty on error, so behaviour is identical).
    fn max_sequence(client: &mut postgres::Client) -> i64 {
        client
            .query(
                "SELECT COALESCE(MAX(sequence), 0) FROM gptbridge_transport.outbox_event",
                &[],
            )
            .ok()
            .and_then(|rows| rows.first().map(|r| r.get::<_, i64>(0)))
            .unwrap_or(-1)
    }

    /// Retention: drop events every connected session already acknowledged,
    /// never going below the newest ``RETENTION_MIN_EVENTS`` rows.  Skipped
    /// outright when ``latest`` shows nothing acked could be prunable —
    /// idle loops must not issue a DELETE every interval.
    fn prune(&self, client: &mut postgres::Client, latest: i64) {
        let min_acked = {
            let sessions = self.sessions.lock().unwrap();
            sessions.values().map(|s| s.acked).min()
        };
        let Some(min_acked) = min_acked else {
            return;
        };
        // Deletable rows exist only when something is below the acked
        // floor AND the table head already exceeds the retention bound —
        // skip the DELETE entirely otherwise.
        if min_acked <= 1 || latest <= RETENTION_MIN_EVENTS {
            return;
        }
        let _ = client.execute(
            "DELETE FROM gptbridge_transport.outbox_event \
             WHERE sequence < $1 AND sequence <= \
             (SELECT COALESCE(MAX(sequence), 0) \
              FROM gptbridge_transport.outbox_event) - $2",
            &[&min_acked, &RETENTION_MIN_EVENTS],
        );
    }
}
