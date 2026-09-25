/* a263_channel_core.c — A263 channel 決定性語義（C23 純語義，I/O 由呼叫方）。
 * C23 upgrade: constexpr, auto, _BitInt for 64-bit generation.
 *
 * 逐函式對齊 shared_layer/channel_runtime.py、connection_mixin.py、
 * heartbeat_mixin.py、transactional_outbox.py 的可觀測行為。
 */
#include "a263_channel_core.h"

#include <stdio.h>
#include <string.h>

const char* gptbridge_a263_state_name(gptbridge_a263_state_t state) {
    switch (state) {
    case GPTBRIDGE_A263_STATE_CLOSED: return "closed";
    case GPTBRIDGE_A263_STATE_CONNECTING: return "connecting";
    case GPTBRIDGE_A263_STATE_OPEN: return "open";
    case GPTBRIDGE_A263_STATE_RECONNECTING: return "reconnecting";
    case GPTBRIDGE_A263_STATE_DEAD: return "dead";
    default: return "unknown";
    }
}

uint64_t gptbridge_a263_next_generation(uint64_t current_generation) {
    return current_generation + 1u;
}

gptbridge_a263_reconnect_verdict_t gptbridge_a263_reconnect_verdict(
    uint32_t attempts_after_increment,
    uint32_t max_attempts) {
    if (attempts_after_increment > max_attempts) {
        return GPTBRIDGE_A263_RECONNECT_DEAD;
    }
    return GPTBRIDGE_A263_RECONNECT_OK;
}

double gptbridge_a263_reconnect_delay_seconds(
    double base_seconds,
    uint32_t attempt) {
    /* Python: delay = base * (2 ** (attempt - 1))；attempt >= 1。 */
    double delay = base_seconds;
    uint32_t exponent = attempt > 0 ? attempt - 1u : 0u;
    uint32_t i;
    for (i = 0; i < exponent; ++i) {
        delay *= 2.0;
    }
    return delay;
}

int gptbridge_a263_heartbeat_expired(
    double now_monotonic,
    double last_pong_received,
    double timeout_seconds) {
    return (now_monotonic - last_pong_received) > timeout_seconds;
}

uint64_t gptbridge_a263_ack_advance(uint64_t current, uint64_t incoming) {
    return incoming > current ? incoming : current;
}

void gptbridge_a263_resync_cursors(
    uint64_t peer_cursor,
    uint64_t* acked_cursor,
    uint64_t* sent_upto) {
    if (acked_cursor != NULL) {
        *acked_cursor = peer_cursor;
    }
    if (sent_upto != NULL) {
        *sent_upto = peer_cursor;
    }
}

int gptbridge_a263_fetch_window(
    uint64_t cursor,
    uint64_t latest_sequence,
    uint32_t limit,
    uint64_t* first_seq,
    uint64_t* end_seq_exclusive) {
    uint64_t first = cursor + 1u;
    uint64_t end = cursor + 1u + (uint64_t)limit;
    if (end > latest_sequence + 1u) {
        end = latest_sequence + 1u;
    }
    if (first_seq != NULL) {
        *first_seq = first;
    }
    if (end_seq_exclusive != NULL) {
        *end_seq_exclusive = end;
    }
    return end > first;
}

uint64_t gptbridge_a263_outbox_next_sequence(uint64_t latest_sequence) {
    return latest_sequence + 1u;
}

int gptbridge_a263_sequence_key(
    const char* channel_id,
    uint64_t sequence,
    char* buf,
    size_t buflen) {
    int written;
    if (channel_id == NULL || buf == NULL || buflen == 0u) {
        return 0;
    }
    written = snprintf(
        buf, buflen, "%s:%llu", channel_id, (unsigned long long)sequence);
    if (written < 0 || (size_t)written >= buflen) {
        return 0;
    }
    return 1;
}

int gptbridge_a263_enqueue_allowed(
    uint32_t queue_len,
    uint32_t capacity,
    int backpressure_enabled) {
    if (!backpressure_enabled) {
        return 1;
    }
    return queue_len < capacity;
}

int gptbridge_a263_drain_order(
    const int32_t* priorities,
    int32_t count,
    int32_t* order_out) {
    int32_t i;
    int32_t j;
    if (priorities == NULL || order_out == NULL || count < 0) {
        return 0;
    }
    for (i = 0; i < count; ++i) {
        order_out[i] = i;
    }
    /* 穩定插入排序：同 priority 保持原 FIFO 順序（Python sort 穩定）。 */
    for (i = 1; i < count; ++i) {
        int32_t key_index = order_out[i];
        int32_t key_priority = priorities[key_index];
        j = i - 1;
        while (j >= 0 && priorities[order_out[j]] > key_priority) {
            order_out[j + 1] = order_out[j];
            --j;
        }
        order_out[j + 1] = key_index;
    }
    return 1;
}

int gptbridge_a263_verify_snapshot(int64_t snapshot_cursor) {
    return snapshot_cursor >= 0;
}
