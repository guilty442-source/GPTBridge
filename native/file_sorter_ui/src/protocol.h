/* protocol.h — governed tool-window command frames.
 *
 * Outbound: {"command": X, "payload": {request_id, ...}}  (text frame)
 * Inbound:  {"event": E, "payload": {...}}                (text frame)
 * Mirrors gptbridge_core::ipc::ws LoopbackSocket semantics. */
#ifndef GPTBRIDGE_FSUI_PROTOCOL_H
#define GPTBRIDGE_FSUI_PROTOCOL_H

#include <string>
#include <vector>

#include "jsonlite.h"

namespace fsui {
namespace proto {

using gptbridge::jsonlite::JsonValue;

/* request_id: "<prefix>-<counter>-<pid>" (backend.rs parity). */
std::string next_request_id();

/* toolbox_run_tool frame for a serial run. */
std::string encode_run_tool(const std::string& request_id,
                            const std::string& tool_id,
                            const std::vector<std::string>& args,
                            unsigned timeout_s);

/* toolbox_cancel_tool_run frame. */
std::string encode_cancel_run(const std::string& tool_id,
                              const std::string& request_id);

/* Inbound event extraction: returns "" when not an event frame;
 * *payload receives payload object (Null when absent). */
std::string decode_event(const std::string& frame, JsonValue* payload);

/* helpers on JsonValue */
const JsonValue* jget(const JsonValue& v, const char* key);
std::string jstr(const JsonValue& v, const char* key);
double jnum(const JsonValue& v, const char* key);
bool jbool(const JsonValue& v, const char* key, bool fallback = false);
std::string jescape(const std::string& s);

} // namespace proto
} // namespace fsui
#endif /* GPTBRIDGE_FSUI_PROTOCOL_H */
