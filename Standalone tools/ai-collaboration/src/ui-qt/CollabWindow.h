#pragma once

#include <QJsonArray>
#include <QJsonObject>
#include <QMainWindow>
#include <QSet>

class BackendSocket;
class BrowserOpsHandler;
class QButtonGroup;
class QComboBox;
class QLabel;
class QLineEdit;
class QPlainTextEdit;
class QProgressBar;
class QPushButton;
class QScrollArea;
class QStackedWidget;
class QTimer;
class QVBoxLayout;
class QWidget;

// CollabWindow — the ai-collaboration tool window (Qt Widgets shell +
// Qt WebEngine embedded browser). UI-facing parity with the retired
// React surface: agent rail, collab composer, responses feed, manual
// import, settings drawer, governed socket + browser-op bridge.
class CollabWindow : public QMainWindow {
    Q_OBJECT
public:
    CollabWindow(const QString &toolId, const QString &wsUrl,
                 QWidget *parent = nullptr);

private slots:
    void onFrame(const QString &event, const QJsonObject &payload);
    void onSocketConnected(bool connected);
    void resync();

private:
    // layout builders
    QWidget *buildHeader();
    QWidget *buildRail();
    QWidget *buildComposer();
    QWidget *buildFeed();
    QWidget *buildBrowserPane();

    // renderers
    void renderAgents();
    void renderFeed();
    void renderTask(const QJsonObject &task);
    QWidget *messageCard(const QJsonObject &message);
    QWidget *responseRow(const QJsonObject &message, const QJsonObject &response);
    QWidget *comparisonBlock(const QJsonObject &comparison);
    QWidget *synthesisBlock(const QJsonObject &synthesis);

    // actions
    void send();
    void cancelTask(const QString &taskId);
    void manualImport(const QString &taskId, const QString &providerId);
    void completeBrowserResponse(const QString &messageId, const QString &agentId);
    void openAgent(const QString &agentId);
    void authorizeAgent(const QString &agentId);
    void exportReport();
    void showSettings();
    void applyTemplate(const QString &kind);

    // state helpers
    void applyState(const QJsonObject &state);
    QStringList selectedProviderIds() const;
    void setBusy(bool busy);
    void setError(const QString &message);
    void flash(const QString &message);

    QString toolId_;
    QString wsUrl_;

    BackendSocket *socket_ = nullptr;
    BrowserOpsHandler *browserOps_ = nullptr;
    QTimer *resyncTimer_ = nullptr;

    // state (state store parity)
    QJsonArray agents_;
    QJsonArray messages_;
    QJsonArray collabTasks_;
    QJsonArray memoryItems_;
    QJsonObject providerExecution_;
    QString currentTaskId_;
    QSet<QString> selectedAgents_;
    bool busy_ = false;

    // widgets
    QLabel *connPill_ = nullptr;
    QLabel *statusLine_ = nullptr;
    QLabel *errorLabel_ = nullptr;
    QWidget *railContent_ = nullptr;
    QVBoxLayout *feedLayout_ = nullptr;
    QWidget *taskSlot_ = nullptr;
    QStackedWidget *browserStack_ = nullptr;
    QLineEdit *urlEdit_ = nullptr;
    QProgressBar *loadBar_ = nullptr;
    QComboBox *singleProvider_ = nullptr;
    QButtonGroup *modeGroup_ = nullptr;
    QPlainTextEdit *promptEdit_ = nullptr;
    QPushButton *sendBtn_ = nullptr;
    QPushButton *cancelBtn_ = nullptr;
    QComboBox *sessionPicker_ = nullptr;
};
