/* ws_client.cpp — RFC 6455 client implementation. */
#include "ws_client.h"

#include <cstdint>
#include <cstdlib>
#include <winsock2.h>
#include <ws2tcpip.h>

#pragma comment(lib, "ws2_32.lib")

namespace fsui {

namespace {

struct WsaInit {
    WsaInit() { WSADATA d; WSAStartup(MAKEWORD(2, 2), &d); }
    ~WsaInit() { WSACleanup(); }
};

std::string b64_encode(const uint8_t* data, size_t n) {
    static const char* tbl =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    for (size_t i = 0; i < n; i += 3) {
        uint32_t v = (uint32_t)data[i] << 16;
        if (i + 1 < n) v |= (uint32_t)data[i + 1] << 8;
        if (i + 2 < n) v |= data[i + 2];
        out += tbl[(v >> 18) & 63];
        out += tbl[(v >> 12) & 63];
        out += i + 1 < n ? tbl[(v >> 6) & 63] : '=';
        out += i + 2 < n ? tbl[v & 63] : '=';
    }
    return out;
}

/* ws://127.0.0.1:PORT/target parse; loopback only (fail-closed). */
bool parse_url(const std::string& url, std::string* port, std::string* target) {
    const std::string scheme = "ws://127.0.0.1:";
    if (url.rfind(scheme, 0) != 0) return false;
    std::string rest = url.substr(scheme.size());
    auto slash = rest.find('/');
    std::string port_s = slash == std::string::npos ? rest : rest.substr(0, slash);
    if (port_s.empty()) return false;
    for (char c : port_s) if (c < '0' || c > '9') return false;
    *port = port_s;
    *target = slash == std::string::npos ? "/" : rest.substr(slash);
    return true;
}

} // namespace

void WsClient::init(HWND hwnd, UINT socket_msg, OnMessage on_message, OnState on_state) {
    static WsaInit wsa;
    hwnd_ = hwnd;
    socket_msg_ = socket_msg;
    on_message_ = std::move(on_message);
    on_state_ = std::move(on_state);
}

void WsClient::set_state(State s) {
    if (state_ == s) return;
    state_ = s;
    if (on_state_) on_state_(s);
}

bool WsClient::connect(const std::string& url) {
    close();
    std::string port, target;
    if (!parse_url(url, &port, &target)) return false;
    target_ = target;
    authority_ = "127.0.0.1:" + port;

    sock_ = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (sock_ == INVALID_SOCKET) return false;
    if (WSAAsyncSelect(sock_, hwnd_, socket_msg_,
                       FD_CONNECT | FD_READ | FD_WRITE | FD_CLOSE) == SOCKET_ERROR) {
        fail();
        return false;
    }
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons((u_short)std::strtoul(port.c_str(), nullptr, 10));
    set_state(State::Connecting);
    if (::connect(sock_, (sockaddr*)&addr, sizeof(addr)) == SOCKET_ERROR &&
        WSAGetLastError() != WSAEWOULDBLOCK) {
        fail();
        return false;
    }
    return true;
}

void WsClient::begin_handshake() {
    uint8_t nonce[16];
    for (auto& b : nonce) b = (uint8_t)(std::rand() & 0xFF);
    expect_key_ = b64_encode(nonce, sizeof(nonce));
    std::string req =
        "GET " + target_ + " HTTP/1.1\r\n"
        "Host: " + authority_ + "\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: " + expect_key_ + "\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n";
    outbuf_ += req;
    pump_send();
    set_state(State::Handshaking);
}

void WsClient::pump_send() {
    while (!outbuf_.empty()) {
        int n = send(sock_, outbuf_.data(), (int)outbuf_.size(), 0);
        if (n == SOCKET_ERROR) {
            if (WSAGetLastError() == WSAEWOULDBLOCK) return;
            fail();
            return;
        }
        outbuf_.erase(0, (size_t)n);
    }
}

void WsClient::pump_recv() {
    char buf[16384];
    for (;;) {
        int n = recv(sock_, buf, sizeof(buf), 0);
        if (n <= 0) {
            if (n == 0 || WSAGetLastError() != WSAEWOULDBLOCK) fail();
            return;
        }
        inbuf_.append(buf, (size_t)n);
        if (state_ == State::Handshaking) {
            auto end = inbuf_.find("\r\n\r\n");
            if (end == std::string::npos) {
                if (inbuf_.size() > 16384) fail(); /* header cap */
                continue;
            }
            bool ok = inbuf_.rfind("HTTP/1.1 101", 0) == 0;
            std::string head = inbuf_.substr(0, end);
            bool accept_ok = head.find("Sec-WebSocket-Accept:") != std::string::npos;
            inbuf_.erase(0, end + 4);
            if (!ok || !accept_ok) { fail(); return; }
            set_state(State::Connected);
        }
        if (state_ == State::Connected && !try_parse_frames()) {
            fail();
            return;
        }
        if (n < (int)sizeof(buf)) return;
    }
}

/* Client-perspective inbound decode: frames are UNMASKED.
 * Returns false on protocol error. */
bool WsClient::try_parse_frames() {
    for (;;) {
        if (inbuf_.size() < 2) return true;
        const uint8_t* p = (const uint8_t*)inbuf_.data();
        bool fin = (p[0] & 0x80) != 0;
        uint8_t op = p[0] & 0x0F;
        bool masked = (p[1] & 0x80) != 0;
        uint64_t len = p[1] & 0x7F;
        size_t hdr = 2;
        if (masked) return false; /* server must not mask */
        if (len == 126) {
            if (inbuf_.size() < 4) return true;
            len = ((uint64_t)p[2] << 8) | p[3];
            hdr = 4;
        } else if (len == 127) {
            if (inbuf_.size() < 10) return true;
            len = 0;
            for (int i = 0; i < 8; ++i) len = (len << 8) | p[2 + i];
            hdr = 10;
        }
        if (len > kMaxInbound) return false;
        if (op >= 0x8 && (!fin || len > 125)) return false;
        if (inbuf_.size() < hdr + len) return true;
        std::string payload = inbuf_.substr(hdr, (size_t)len);
        inbuf_.erase(0, hdr + (size_t)len);
        switch (op) {
            case 0x0: /* continuation */
                fragment_ += payload;
                if (fin) {
                    if (on_message_) on_message_(fragment_);
                    fragment_.clear();
                }
                break;
            case 0x1: /* text */
                if (fin) { if (on_message_) on_message_(payload); }
                else fragment_ = payload;
                break;
            case 0x2: /* binary: ignore (protocol sends text only) */
                break;
            case 0x8: /* close */ return false;
            case 0x9: { /* ping -> pong (masked outbound) */
                std::string frame;
                frame += (char)0x8A;
                if (payload.size() <= 125) {
                    frame += (char)(0x80 | payload.size());
                } else {
                    frame += (char)(0x80 | 126);
                    frame += (char)(payload.size() >> 8);
                    frame += (char)(payload.size() & 0xFF);
                }
                uint8_t mask[4];
                for (auto& b : mask) b = (uint8_t)(std::rand() & 0xFF);
                frame.append((const char*)mask, 4);
                for (size_t i = 0; i < payload.size(); ++i)
                    frame += (char)(payload[i] ^ mask[i & 3]);
                outbuf_ += frame;
                pump_send();
                break;
            }
            case 0xA: /* pong: ignore */ break;
            default: return false;
        }
        if (state_ != State::Connected) return true;
    }
}

bool WsClient::send_text(const std::string& payload) {
    if (state_ != State::Connected || sock_ == INVALID_SOCKET) return false;
    std::string frame;
    frame += (char)0x81; /* FIN + text */
    uint64_t len = payload.size();
    if (len <= 125) {
        frame += (char)(0x80 | len);
    } else if (len <= 0xFFFF) {
        frame += (char)(0x80 | 126);
        frame += (char)(len >> 8);
        frame += (char)(len & 0xFF);
    } else {
        frame += (char)(0x80 | 127);
        for (int i = 7; i >= 0; --i) frame += (char)((len >> (i * 8)) & 0xFF);
    }
    uint8_t mask[4];
    for (auto& b : mask) b = (uint8_t)(std::rand() & 0xFF);
    frame.append((const char*)mask, 4);
    for (uint64_t i = 0; i < len; ++i)
        frame += (char)(payload[i] ^ mask[i & 3]);
    outbuf_ += frame;
    pump_send();
    return true;
}

void WsClient::on_socket_event(WPARAM, LPARAM lparam) {
    int ev = WSAGETSELECTEVENT(lparam);
    int err = WSAGETSELECTERROR(lparam);
    if (err != 0) { fail(); return; }
    switch (ev) {
        case FD_CONNECT:
            if (state_ == State::Connecting) begin_handshake();
            break;
        case FD_READ:  pump_recv(); break;
        case FD_WRITE: pump_send(); break;
        case FD_CLOSE: fail(); break;
    }
}

void WsClient::fail() {
    close();
    set_state(State::Disconnected);
}

void WsClient::close() {
    if (sock_ != INVALID_SOCKET) {
        WSAAsyncSelect(sock_, hwnd_, 0, 0);
        closesocket(sock_);
        sock_ = INVALID_SOCKET;
    }
    inbuf_.clear();
    outbuf_.clear();
}

} // namespace fsui
