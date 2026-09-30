/* ws_client.h — minimal RFC 6455 client over WinSock2.
 *
 * Single-threaded: the socket is bound to the UI thread's message
 * loop via WSAAsyncSelect; all callbacks fire inside the window
 * procedure. Client-side rules: outbound frames are masked,
 * inbound frames are unmasked, ping -> pong, close -> Closed event.
 * Loopback only (ws://127.0.0.1:port/?...). */
#ifndef GPTBRIDGE_FSUI_WS_CLIENT_H
#define GPTBRIDGE_FSUI_WS_CLIENT_H

#include <functional>
#include <string>
#include <winsock2.h>
#include <windows.h>

namespace fsui {

class WsClient {
public:
    enum class State { Disconnected, Connecting, Handshaking, Connected };

    /* on_message: one full JSON text frame (UTF-8).
     * on_state:   fires on every State transition. */
    using OnMessage = std::function<void(const std::string&)>;
    using OnState   = std::function<void(State)>;

    WsClient() = default;
    ~WsClient() { close(); }

    void init(HWND hwnd, UINT socket_msg, OnMessage on_message, OnState on_state);
    /* url: ws://127.0.0.1:<port>/<target>. Non-blocking connect;
     * state transitions arrive via on_state / socket_msg. */
    bool connect(const std::string& url);
    /* Queue a masked text frame; returns false when not connected. */
    bool send_text(const std::string& payload);
    void close();
    State state() const { return state_; }

    /* Call from the window procedure on socket_msg. */
    void on_socket_event(WPARAM wparam, LPARAM lparam);

private:
    static constexpr size_t kMaxInbound = 1u << 20; /* 1 MiB frame cap */

    void set_state(State s);
    void begin_handshake();
    void pump_recv();
    void pump_send();
    bool try_parse_frames();
    void fail();

    HWND hwnd_ = nullptr;
    UINT socket_msg_ = 0;
    SOCKET sock_ = INVALID_SOCKET;
    State state_ = State::Disconnected;
    std::string target_;          /* request-target incl. query */
    std::string authority_;       /* Host header */
    std::string inbuf_;
    std::string outbuf_;
    std::string fragment_;        /* continuation assembly across recvs */
    std::string expect_key_;      /* Sec-WebSocket-Key we sent */
    OnMessage on_message_;
    OnState on_state_;
};

} // namespace fsui
#endif /* GPTBRIDGE_FSUI_WS_CLIENT_H */
