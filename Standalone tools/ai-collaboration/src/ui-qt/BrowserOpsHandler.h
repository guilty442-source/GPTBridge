#pragma once

#include <QHash>
#include <QJsonObject>
#include <QObject>
#include <QStackedWidget>
#include <QString>
#include <QTimer>
#include <QWebEngineProfile>
#include <QWebEngineView>

class BackendSocket;

// BrowserOpsHandler executes the ai_collab_browser_op events delegated
// by the Go backend against per-agent QWebEngineViews that share one
// QWebEngineProfile (shared foreground browser context — parity with
// the retired embedded-browser contract: browser-only, no provider
// APIs, no cross-provider substitution).
class BrowserOpsHandler : public QObject {
    Q_OBJECT
public:
    BrowserOpsHandler(QStackedWidget *stack, QWidget *placeholder,
                      BackendSocket *socket, QObject *parent = nullptr);
    ~BrowserOpsHandler() override;

    QWebEngineView *activeView() const;
    QString activeSessionId() const { return activeSession_; }
    QStringList sessionIds() const { return views_.keys(); }

public slots:
    // Handles one {"op_id","op","session_id","url","script"} payload.
    void handleOp(const QJsonObject &payload);
    void activateSession(const QString &sessionId);
    void showPlaceholder();

signals:
    void activeSessionUrlChanged(const QString &sessionId, const QString &url);
    void activeSessionTitleChanged(const QString &title);
    void loadProgressChanged(int percent);
    void loadFinishedChanged(bool ok);

private:
    void reply(const QString &opId, const QJsonObject &result);
    QWebEngineView *viewFor(const QString &sessionId) const;
    QWebEngineView *createView(const QString &sessionId, const QString &url);
    void activate(QWebEngineView *view, const QString &sessionId);

    QWebEngineProfile *profile_; // shared browser context (persistent)
    QStackedWidget *stack_;
    QWidget *placeholder_;
    BackendSocket *socket_;
    QHash<QString, QWebEngineView *> views_;
    QString activeSession_;
};
