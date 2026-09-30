#include "BackendSocket.h"

#include <QJsonDocument>
#include <QRandomGenerator>
#include <QUuid>

namespace {
constexpr int kReconnectMaxDelayMs = 8000;

QString mutationId(const QString &command) {
    return command + QLatin1Char(':') +
           QUuid::createUuid().toString(QUuid::WithoutBraces);
}
} // namespace

BackendSocket::BackendSocket(QObject *parent) : QObject(parent) {
    reconnect_ = new QTimer(this);
    reconnect_->setSingleShot(true);
    connect(reconnect_, &QTimer::timeout, this, &BackendSocket::connectNow);

    connect(&ws_, &QWebSocket::connected, this, [this] {
        attempt_ = 0;
        reconnect_->stop();
        setStatus(QStringLiteral("Connected"));
        emit socketConnected(true);
    });
    connect(&ws_, &QWebSocket::disconnected, this, [this] {
        if (disposed_)
            return;
        setStatus(QStringLiteral("Disconnected"));
        emit socketConnected(false);
        scheduleReconnect();
    });
    connect(&ws_, &QWebSocket::textMessageReceived, this,
            [this](const QString &message) {
                const QJsonDocument doc =
                    QJsonDocument::fromJson(message.toUtf8());
                if (!doc.isObject())
                    return;
                const QJsonObject frame = doc.object();
                const QString event = frame.value(QStringLiteral("event")).toString();
                if (event.isEmpty())
                    return;
                emit frameReceived(event,
                                   frame.value(QStringLiteral("payload")).toObject());
            });
    connect(&ws_, &QWebSocket::errorOccurred, this, [this](QAbstractSocket::SocketError) {
        if (disposed_)
            return;
        setStatus(QStringLiteral("Error: ") + ws_.errorString());
        if (ws_.state() == QAbstractSocket::ConnectedState ||
            ws_.state() == QAbstractSocket::ConnectingState)
            ws_.close();
    });
}

void BackendSocket::start(const QUrl &url) {
    url_ = url;
    disposed_ = false;
    connectNow();
}

void BackendSocket::shutdown() {
    disposed_ = true;
    reconnect_->stop();
    if (ws_.state() == QAbstractSocket::ConnectedState ||
        ws_.state() == QAbstractSocket::ConnectingState)
        ws_.close();
}

void BackendSocket::sendCommand(const QString &command, const QJsonObject &payload) {
    QJsonObject prepared = payload;
    if (prepared.value(QStringLiteral("idempotency_key")).toString().trimmed().isEmpty()) {
        QString key = prepared.value(QStringLiteral("operation_id")).toString().trimmed();
        if (key.isEmpty())
            key = prepared.value(QStringLiteral("request_id")).toString().trimmed();
        if (key.isEmpty())
            key = mutationId(command);
        prepared.insert(QStringLiteral("idempotency_key"), key);
    }
    if (!isOpen()) {
        QJsonObject failure{
            {QStringLiteral("ok"), false},
            {QStringLiteral("queued"), false},
            {QStringLiteral("message"),
             QStringLiteral("後端連線尚未就緒，指令未送出，請稍後再試。")},
            {QStringLiteral("request_id"),
             prepared.value(QStringLiteral("request_id")).toString()},
        };
        emit frameReceived(command + QStringLiteral("_result"), failure);
        return;
    }
    QJsonObject frame{
        {QStringLiteral("command"), command},
        {QStringLiteral("payload"), prepared},
    };
    ws_.sendTextMessage(QString::fromUtf8(
        QJsonDocument(frame).toJson(QJsonDocument::Compact)));
}

void BackendSocket::setStatus(const QString &status) {
    if (status_ == status)
        return;
    status_ = status;
    emit statusChanged(status_);
}

void BackendSocket::connectNow() {
    if (disposed_ || url_.isEmpty())
        return;
    if (ws_.state() == QAbstractSocket::ConnectedState ||
        ws_.state() == QAbstractSocket::ConnectingState)
        return;
    setStatus(QStringLiteral("Connecting"));
    ws_.open(url_);
}

void BackendSocket::scheduleReconnect() {
    if (disposed_ || reconnect_->isActive())
        return;
    attempt_ += 1;
    int base = 500 << (attempt_ - 1);
    if (base > kReconnectMaxDelayMs)
        base = kReconnectMaxDelayMs;
    const int jitter =
        QRandomGenerator::global()->bounded(static_cast<int>(base * 0.2) + 1);
    reconnect_->start(base + jitter);
}
