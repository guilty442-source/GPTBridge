#pragma once

#include <QDateTime>
#include <QJsonArray>
#include <QJsonObject>
#include <QMainWindow>
#include <QVector>

class BackendSocket;
class QComboBox;
class QLabel;
class QLineEdit;
class QPushButton;
class QScrollArea;
class QTimer;
class QVBoxLayout;
class QWidget;

// StarChatWindow — the model-dialogue tool window. Qt Widgets port of
// the egui star-chat surface (tool_window/star_chat.rs): sidebar with
// connection state, model picker fed by star_chat_status, turn list
// with streaming updates, composer with diagnostics actions, governed
// stop via toolbox_cancel_tool_run.
class StarChatWindow : public QMainWindow {
    Q_OBJECT
public:
    StarChatWindow(const QString &toolId, const QString &wsUrl,
                   QWidget *parent = nullptr);

private slots:
    void onFrame(const QString &event, const QJsonObject &payload);
    void onSocketConnected(bool connected);
    void pollStatus();

private:
    struct Turn {
        QString role;
        QString content;
        QString model;
        bool failed = false;
    };
    struct Pending {
        QString requestId;
        QString command;
        bool streamed = false;
        int assistantIndex = -1;
    };

    // layout builders
    QWidget *buildSidebar();
    QWidget *buildHeader();
    QWidget *buildComposer();

    // renderers
    QWidget *turnCard(int index);
    void appendTurn(const QString &role, const QString &content,
                    const QString &model = {}, bool failed = false);
    void updateCard(int index);
    void clearTurns();
    void scrollToEnd();

    // actions
    void sendMessage();
    void runDiagnostic(const QString &command, const QString &label);
    void stopGenerating();

    // protocol helpers
    QString sendRequest(const QString &command, const QJsonObject &payload);
    void finishPending(const QJsonObject &payload);
    void handleProgress(const QJsonObject &payload);
    void applyStatus(const QJsonObject &payload);
    void failPending(const QString &message);
    void refreshComposer();
    void refreshStage();

    QString toolId_;
    QString wsUrl_;

    BackendSocket *socket_ = nullptr;
    QTimer *statusTimer_ = nullptr;
    QTimer *tickTimer_ = nullptr;

    QVector<Turn> turns_;
    QVector<QWidget *> cards_;
    bool generating_ = false;
    QString stage_ = QStringLiteral("準備處理");
    QDateTime thinkingSince_;
    Pending *pending_ = nullptr;
    QVector<QPair<QString, QString>> models_;  // (name, label)
    QString selectedModel_;
    quint64 requestSeq_ = 0;

    // widgets
    QLabel *connDot_ = nullptr;
    QLabel *connLabel_ = nullptr;
    QComboBox *modelPicker_ = nullptr;
    QPushButton *clearBtn_ = nullptr;
    QPushButton *diagAlignBtn_ = nullptr;
    QPushButton *diagSyncBtn_ = nullptr;
    QLineEdit *draftEdit_ = nullptr;
    QPushButton *sendBtn_ = nullptr;
    QPushButton *stopBtn_ = nullptr;
    QLabel *stageLabel_ = nullptr;
    QScrollArea *chatScroll_ = nullptr;
    QWidget *chatContent_ = nullptr;
    QVBoxLayout *chatLayout_ = nullptr;
    QWidget *emptyState_ = nullptr;
};
