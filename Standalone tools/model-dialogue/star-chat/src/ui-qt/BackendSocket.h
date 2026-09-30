#pragma once

#include <QJsonObject>
#include <QObject>
#include <QTimer>
#include <QUrl>
#include <QWebSocket>

// Governed backend WebSocket client — parity with
// shared-layer/src/ui/toolWindow/localBackendSocket.js and the egui
// tool_window backend: loopback session-authenticated socket,
// {command,payload} / {event,payload} frames, idempotency-key
// injection, no offline queueing, bounded exponential reconnect
// (<= 8 s). Answers heartbeat_ping inline with heartbeat_pong — the
// governed channel culls sockets silent past HEARTBEAT_TIMEOUT.
class BackendSocket : public QObject {
    Q_OBJECT
public:
    explicit BackendSocket(QObject *parent = nullptr);

    void start(const QUrl &url);
    void shutdown();

    // Emits a local <command>_result failure frame when the socket is
    // not open — matching dispatchLocalFailure in the JS client.
    void sendCommand(const QString &command, const QJsonObject &payload);

    QString status() const { return status_; }
    bool isOpen() const { return ws_.state() == QAbstractSocket::ConnectedState; }

signals:
    void statusChanged(const QString &status);
    void frameReceived(const QString &event, const QJsonObject &payload);
    void socketConnected(bool connected);

private:
    void setStatus(const QString &status);
    void connectNow();
    void scheduleReconnect();

    QWebSocket ws_;
    QUrl url_;
    QTimer *reconnect_ = nullptr;
    int attempt_ = 0;
    bool disposed_ = false;
    QString status_ = QStringLiteral("Disconnected");
};
