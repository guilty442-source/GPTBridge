#include "BrowserOpsHandler.h"

#include "BackendSocket.h"

#include <QJsonDocument>
#include <QJsonValue>
#include <QPointer>
#include <QVariant>
#include <QWebEnginePage>

namespace {
constexpr int kOpReplyTimeoutMs = 30000;

QJsonObject okResult() { return QJsonObject{{QStringLiteral("ok"), true}}; }

QJsonObject failResult(const QString &code, const QString &message) {
    return QJsonObject{
        {QStringLiteral("ok"), false},
        {QStringLiteral("message"), message.isEmpty() ? code : message},
        {QStringLiteral("error_code"), code},
    };
}
} // namespace

BrowserOpsHandler::BrowserOpsHandler(QStackedWidget *stack, QWidget *placeholder,
                                     BackendSocket *socket, QObject *parent)
    : QObject(parent), stack_(stack), placeholder_(placeholder),
      socket_(socket) {
    // Shared foreground context: one persistent profile across all
    // provider sessions (cookies/logins persist — same semantics as
    // the retired shared webview partition).
    profile_ = QWebEngineProfile::defaultProfile();
}

BrowserOpsHandler::~BrowserOpsHandler() {
    for (QWebEngineView *view : views_)
        view->deleteLater();
}

QWebEngineView *BrowserOpsHandler::viewFor(const QString &sessionId) const {
    return views_.value(sessionId, nullptr);
}

QWebEngineView *BrowserOpsHandler::activeView() const {
    if (activeSession_.isEmpty())
        return nullptr;
    return views_.value(activeSession_, nullptr);
}

void BrowserOpsHandler::activateSession(const QString &sessionId) {
    if (QWebEngineView *view = viewFor(sessionId))
        activate(view, sessionId);
}

void BrowserOpsHandler::showPlaceholder() {
    stack_->setCurrentWidget(placeholder_);
}

void BrowserOpsHandler::reply(const QString &opId, const QJsonObject &result) {
    QJsonObject payload = result;
    payload.insert(QStringLiteral("op_id"), opId);
    socket_->sendCommand(QStringLiteral("ai_collab_browser_op_result"), payload);
}

void BrowserOpsHandler::activate(QWebEngineView *view, const QString &sessionId) {
    activeSession_ = sessionId;
    stack_->setCurrentWidget(view);
    emit activeSessionUrlChanged(sessionId, view->url().toString());
    emit activeSessionTitleChanged(view->title());
}

QWebEngineView *BrowserOpsHandler::createView(const QString &sessionId,
                                              const QString &url) {
    auto *page = new QWebEnginePage(profile_, this);
    auto *view = new QWebEngineView;
    view->setPage(page);
    view->setContextMenuPolicy(Qt::DefaultContextMenu);
    views_.insert(sessionId, view);
    stack_->addWidget(view);

    connect(view, &QWebEngineView::urlChanged, this,
            [this, sessionId](const QUrl &u) {
                if (sessionId == activeSession_)
                    emit activeSessionUrlChanged(sessionId, u.toString());
            });
    connect(view, &QWebEngineView::titleChanged, this,
            [this, sessionId](const QString &title) {
                if (sessionId == activeSession_)
                    emit activeSessionTitleChanged(title);
            });
    connect(view, &QWebEngineView::loadProgress, this,
            [this, sessionId](int percent) {
                if (sessionId == activeSession_)
                    emit loadProgressChanged(percent);
            });
    connect(view, &QWebEngineView::loadFinished, this,
            [this, sessionId](bool ok) {
                if (sessionId == activeSession_)
                    emit loadFinishedChanged(ok);
            });

    if (!url.isEmpty())
        view->load(QUrl(url));
    return view;
}

void BrowserOpsHandler::handleOp(const QJsonObject &payload) {
    const QString opId = payload.value(QStringLiteral("op_id")).toString();
    const QString op = payload.value(QStringLiteral("op")).toString();
    const QString sessionId = payload.value(QStringLiteral("session_id")).toString();
    const QString url = payload.value(QStringLiteral("url")).toString();
    const QString script = payload.value(QStringLiteral("script")).toString();
    if (opId.isEmpty() || sessionId.isEmpty())
        return;

    if (op == QLatin1String("create")) {
        QWebEngineView *view = viewFor(sessionId);
        if (!view)
            view = createView(sessionId, url);
        else if (!url.isEmpty() && view->url().isEmpty())
            view->load(QUrl(url));
        activate(view, sessionId);
        QJsonObject res = okResult();
        res.insert(QStringLiteral("id"), sessionId);
        res.insert(QStringLiteral("backend"), QStringLiteral("qt-webengine"));
        reply(opId, res);
        return;
    }

    QWebEngineView *view = viewFor(sessionId);
    if (!view) {
        reply(opId, failResult(QStringLiteral("SESSION_NOT_FOUND"),
                               QStringLiteral("session not found")));
        return;
    }

    if (op == QLatin1String("navigate")) {
        if (url.isEmpty()) {
            reply(opId, failResult(QStringLiteral("INVALID_URL"),
                                   QStringLiteral("url is required")));
            return;
        }
        auto *timeout = new QTimer(this);
        timeout->setSingleShot(true);
        auto conn = std::make_shared<QMetaObject::Connection>();
        auto finish = [this, opId, sessionId, view, timeout,
                       conn](bool ok, bool timedOut) {
            QObject::disconnect(*conn);
            timeout->stop();
            timeout->deleteLater();
            if (timedOut) {
                reply(opId, failResult(
                                QStringLiteral("NAVIGATE_TIMEOUT"),
                                QStringLiteral("page load timed out")));
                return;
            }
            if (views_.value(sessionId) == view)
                activate(view, sessionId);
            QJsonObject res = okResult();
            res.insert(QStringLiteral("ok"), ok);
            res.insert(QStringLiteral("url"), view->url().toString());
            reply(opId, res);
        };
        *conn = QObject::connect(view, &QWebEngineView::loadFinished, this,
                                 [finish](bool ok) { finish(ok, false); });
        QObject::connect(timeout, &QTimer::timeout, this,
                         [finish] { finish(false, true); });
        QObject::connect(view, &QObject::destroyed, this,
                         [finish] { finish(false, false); });
        timeout->start(kOpReplyTimeoutMs);
        view->load(QUrl(url));
        return;
    }

    if (op == QLatin1String("exec")) {
        if (script.isEmpty()) {
            reply(opId, failResult(QStringLiteral("INVALID_SCRIPT"),
                                   QStringLiteral("script is required")));
            return;
        }
        QPointer<QWebEngineView> guard(view);
        view->page()->runJavaScript(
            script,
            [this, opId, sessionId, guard](const QVariant &value) {
                if (guard && views_.value(sessionId) == guard)
                    activate(guard, sessionId);
                QJsonObject res = okResult();
                res.insert(QStringLiteral("result"),
                           QJsonValue::fromVariant(value));
                reply(opId, res);
            });
        return;
    }

    if (op == QLatin1String("url")) {
        QJsonObject res = okResult();
        res.insert(QStringLiteral("url"), view->url().toString());
        reply(opId, res);
        return;
    }

    if (op == QLatin1String("close")) {
        stack_->removeWidget(view);
        views_.remove(sessionId);
        if (activeSession_ == sessionId) {
            activeSession_.clear();
            showPlaceholder();
        }
        view->deleteLater();
        reply(opId, okResult());
        return;
    }

    reply(opId, failResult(QStringLiteral("UNSUPPORTED_OP"),
                           QStringLiteral("unsupported op: ") + op));
}
