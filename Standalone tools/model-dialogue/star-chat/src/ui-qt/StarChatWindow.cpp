#include "StarChatWindow.h"

#include "BackendSocket.h"

#include <QApplication>
#include <QComboBox>
#include <QFrame>
#include <QHBoxLayout>
#include <QLabel>
#include <QLineEdit>
#include <QProcess>
#include <QPushButton>
#include <QScrollArea>
#include <QScrollBar>
#include <QSplitter>
#include <QTimer>
#include <QVBoxLayout>

namespace {
constexpr int kStatusRefreshMs = 15000;
constexpr int kUiTickMs = 500;
constexpr int kHistoryTurns = 8;

const char *kStyle = R"(
QMainWindow, QDialog { background: #0b0e14; }
QWidget { color: #c9d1d9; font-family: "Microsoft JhengHei UI", "Segoe UI"; font-size: 13px; }
QLabel { background: transparent; }
QLabel[muted="true"] { color: #8b949e; }
QLabel[small="true"] { font-size: 11px; }
QLabel[heading="true"] { font-size: 18px; font-weight: 600; }
QPushButton {
    background: #21262d; border: 1px solid #30363d; border-radius: 6px;
    padding: 6px 14px; color: #c9d1d9;
}
QPushButton:hover { background: #30363d; }
QPushButton:disabled { color: #6e7681; background: #161b22; }
QPushButton[accent="true"] {
    background: #1f6feb; border-color: #1f6feb; color: #ffffff;
}
QPushButton[accent="true"]:hover { background: #388bfd; }
QPushButton[accent="true"]:disabled {
    background: #21262d; border-color: #30363d; color: #6e7681;
}
QPushButton[stop="true"] {
    background: #5c1f1f; border-color: #8b3030; color: #ffb3b3;
}
QLineEdit {
    background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
    padding: 8px 10px; color: #c9d1d9;
}
QLineEdit:focus { border-color: #58a6ff; }
QComboBox {
    background: #21262d; border: 1px solid #30363d; border-radius: 6px;
    padding: 5px 10px; min-width: 180px;
}
QComboBox QAbstractItemView {
    background: #161b22; border: 1px solid #30363d;
    selection-background-color: #1f6feb;
}
QFrame[card="user"] {
    background: #1a2740; border-radius: 6px;
}
QFrame[card="assistant"] {
    background: #152b20; border-radius: 6px;
}
QFrame[card="failed"] {
    background: #2b1518; border-radius: 6px;
}
QScrollArea { border: none; }
)";

QLabel *muted(const QString &text, QWidget *parent, bool small = false) {
    auto *l = new QLabel(text, parent);
    l->setProperty("muted", true);
    if (small)
        l->setProperty("small", true);
    l->setWordWrap(true);
    return l;
}
} // namespace

StarChatWindow::StarChatWindow(const QString &toolId, const QString &wsUrl,
                               QWidget *parent)
    : QMainWindow(parent), toolId_(toolId), wsUrl_(wsUrl) {
    setStyleSheet(QString::fromLatin1(kStyle));

    auto *split = new QSplitter(this);
    split->addWidget(buildSidebar());
    split->setStretchFactor(0, 0);
    split->setCollapsible(0, false);

    auto *right = new QWidget(this);
    auto *rightLayout = new QVBoxLayout(right);
    rightLayout->setContentsMargins(0, 0, 0, 0);
    rightLayout->setSpacing(0);
    rightLayout->addWidget(buildHeader());

    chatScroll_ = new QScrollArea(right);
    chatScroll_->setWidgetResizable(true);
    chatContent_ = new QWidget(chatScroll_);
    chatLayout_ = new QVBoxLayout(chatContent_);
    chatLayout_->setContentsMargins(16, 16, 16, 16);
    chatLayout_->setSpacing(6);
    chatLayout_->addStretch(1);
    chatScroll_->setWidget(chatContent_);

    emptyState_ = new QWidget(chatContent_);
    {
        auto *l = new QVBoxLayout(emptyState_);
        l->setContentsMargins(0, 24, 0, 0);
        auto *heading = new QLabel(QStringLiteral("現在可以開始聊聊。"),
                                   emptyState_);
        heading->setProperty("heading", true);
        heading->setAlignment(Qt::AlignCenter);
        l->addWidget(heading);
        auto *sub = muted(
            QStringLiteral("問答、討論、整理想法、撰寫內容與程式任務都在對話內完成。"),
            emptyState_);
        sub->setAlignment(Qt::AlignCenter);
        l->addWidget(sub);
        l->addSpacing(10);
        for (const QString &suggestion : {
                 QStringLiteral("幫我整理今天的想法"),
                 QStringLiteral("用簡單方式解釋一個概念"),
                 QStringLiteral("幫我潤飾這段文字"),
                 QStringLiteral("設計一個 Python API 並附測試"),
             }) {
            auto *btn = new QPushButton(suggestion, emptyState_);
            connect(btn, &QPushButton::clicked, this,
                    [this, suggestion] { draftEdit_->setText(suggestion); });
            l->addWidget(btn, 0, Qt::AlignHCenter);
        }
    }
    chatLayout_->insertWidget(0, emptyState_);

    stageLabel_ = muted(QString(), right, true);
    stageLabel_->setContentsMargins(16, 0, 16, 0);
    stageLabel_->setVisible(false);

    rightLayout->addWidget(chatScroll_, 1);
    rightLayout->addWidget(stageLabel_);
    rightLayout->addWidget(buildComposer());
    split->addWidget(right);
    split->setStretchFactor(1, 1);
    setCentralWidget(split);

    socket_ = new BackendSocket(this);
    connect(socket_, &BackendSocket::frameReceived,
            this, &StarChatWindow::onFrame);
    connect(socket_, &BackendSocket::socketConnected,
            this, &StarChatWindow::onSocketConnected);
    connect(socket_, &BackendSocket::statusChanged, this,
            [this](const QString &) {
                static const QHash<QString, QString> labels{
                    {QStringLiteral("Connected"), QStringLiteral("模型服務已連線")},
                    {QStringLiteral("Connecting"), QStringLiteral("等待連線")},
                    {QStringLiteral("Disconnected"), QStringLiteral("連線中斷・自動重試")},
                };
                connLabel_->setText(labels.value(socket_->status(),
                                                 socket_->status()));
                const QString color =
                    socket_->isOpen() ? QStringLiteral("#60c880")
                    : socket_->status() == QLatin1String("Connecting")
                        ? QStringLiteral("#dcb450")
                        : QStringLiteral("#dc6060");
                connDot_->setStyleSheet(
                    QStringLiteral("color:%1").arg(color));
                refreshComposer();
            });

    statusTimer_ = new QTimer(this);
    statusTimer_->setInterval(kStatusRefreshMs);
    connect(statusTimer_, &QTimer::timeout, this, &StarChatWindow::pollStatus);
    statusTimer_->start();

    tickTimer_ = new QTimer(this);
    tickTimer_->setInterval(kUiTickMs);
    connect(tickTimer_, &QTimer::timeout, this, &StarChatWindow::refreshStage);
    tickTimer_->start();

    socket_->start(QUrl(wsUrl_));
}

// ------------------------------------------------------------ layout --

QWidget *StarChatWindow::buildSidebar() {
    auto *side = new QWidget(this);
    side->setFixedWidth(200);
    auto *l = new QVBoxLayout(side);
    l->setContentsMargins(14, 14, 14, 14);
    l->setSpacing(6);
    auto *title = new QLabel(QStringLiteral("模型對話"), side);
    title->setProperty("heading", true);
    l->addWidget(title);
    l->addWidget(muted(QStringLiteral("本機模型對話入口"), side));
    auto *sep = new QFrame(side);
    sep->setFrameShape(QFrame::HLine);
    sep->setStyleSheet(QStringLiteral("color:#30363d"));
    l->addWidget(sep);
    auto *connRow = new QHBoxLayout();
    connDot_ = new QLabel(QStringLiteral("●"), side);
    connDot_->setStyleSheet(QStringLiteral("color:#dc6060"));
    connLabel_ = new QLabel(QStringLiteral("等待連線"), side);
    connRow->addWidget(connDot_);
    connRow->addWidget(connLabel_, 1);
    l->addLayout(connRow);
    auto *sep2 = new QFrame(side);
    sep2->setFrameShape(QFrame::HLine);
    sep2->setStyleSheet(QStringLiteral("color:#30363d"));
    l->addWidget(sep2);
    l->addWidget(muted(QStringLiteral("外部協作已停用"), side, true));
    l->addWidget(muted(
        QStringLiteral("訓練與能力編成由星澄原生模型內部自行處理"),
        side, true));
    l->addStretch(1);
    return side;
}

QWidget *StarChatWindow::buildHeader() {
    auto *header = new QWidget(this);
    header->setStyleSheet(
        QStringLiteral("border-bottom:1px solid #21262d"));
    auto *l = new QHBoxLayout(header);
    l->setContentsMargins(14, 8, 14, 8);
    l->setSpacing(10);
    auto *tag = muted(QStringLiteral("GOVERNED LOCAL INTELLIGENCE"),
                      header, true);
    l->addWidget(tag);
    l->addStretch(1);
    modelPicker_ = new QComboBox(header);
    modelPicker_->setVisible(false);
    connect(modelPicker_,
            &QComboBox::currentIndexChanged, this, [this](int index) {
                selectedModel_ = modelPicker_->itemData(index).toString();
            });
    l->addWidget(modelPicker_);
    clearBtn_ = new QPushButton(QStringLiteral("清除本次對話"), header);
    clearBtn_->setToolTip(QStringLiteral("清空目前對話紀錄"));
    connect(clearBtn_, &QPushButton::clicked, this, [this] {
        if (!generating_)
            clearTurns();
    });
    l->addWidget(clearBtn_);
    return header;
}

QWidget *StarChatWindow::buildComposer() {
    auto *composer = new QWidget(this);
    composer->setStyleSheet(
        QStringLiteral("border-top:1px solid #21262d"));
    auto *l = new QVBoxLayout(composer);
    l->setContentsMargins(14, 8, 14, 10);
    l->setSpacing(6);

    auto *diagRow = new QHBoxLayout();
    diagAlignBtn_ = new QPushButton(QStringLiteral("法典 × 實作對齊"), composer);
    connect(diagAlignBtn_, &QPushButton::clicked, this, [this] {
        runDiagnostic(QStringLiteral("star_chat_codex_alignment"),
                      QStringLiteral("法典 × 實作對齊檢查"));
    });
    diagSyncBtn_ = new QPushButton(QStringLiteral("法典 × 架構圖同步"), composer);
    connect(diagSyncBtn_, &QPushButton::clicked, this, [this] {
        runDiagnostic(QStringLiteral("star_chat_architecture_sync"),
                      QStringLiteral("中文法典 × 架構圖同步檢查"));
    });
    diagRow->addWidget(diagAlignBtn_);
    diagRow->addWidget(diagSyncBtn_);
    diagRow->addStretch(1);
    l->addLayout(diagRow);

    auto *inputRow = new QHBoxLayout();
    draftEdit_ = new QLineEdit(composer);
    draftEdit_->setPlaceholderText(
        QStringLiteral("輸入想聊的內容、程式需求或開發指令…"));
    connect(draftEdit_, &QLineEdit::returnPressed, this,
            &StarChatWindow::sendMessage);
    sendBtn_ = new QPushButton(QStringLiteral("↑ 送出"), composer);
    sendBtn_->setProperty("accent", true);
    connect(sendBtn_, &QPushButton::clicked, this,
            &StarChatWindow::sendMessage);
    stopBtn_ = new QPushButton(QStringLiteral("■ 停止"), composer);
    stopBtn_->setProperty("stop", true);
    stopBtn_->setVisible(false);
    connect(stopBtn_, &QPushButton::clicked, this,
            &StarChatWindow::stopGenerating);
    inputRow->addWidget(draftEdit_, 1);
    inputRow->addWidget(sendBtn_);
    inputRow->addWidget(stopBtn_);
    l->addLayout(inputRow);

    l->addWidget(muted(
        QStringLiteral("Enter 送出 · 上下文依本機負載自動調整"),
        composer, true));
    return composer;
}

// ------------------------------------------------------------- turns --

void StarChatWindow::appendTurn(const QString &role, const QString &content,
                                const QString &model, bool failed) {
    turns_.append(Turn{role, content, model, failed});
    emptyState_->setVisible(turns_.isEmpty());
    auto *card = turnCard(turns_.size() - 1);
    cards_.append(card);
    chatLayout_->insertWidget(chatLayout_->count() - 1, card);
}

QWidget *StarChatWindow::turnCard(int index) {
    const Turn &turn = turns_[index];
    const bool user = turn.role == QLatin1String("user");
    auto *card = new QFrame(chatContent_);
    card->setProperty("card", turn.failed
        ? QStringLiteral("failed")
        : user ? QStringLiteral("user") : QStringLiteral("assistant"));
    auto *l = new QVBoxLayout(card);
    l->setContentsMargins(10, 8, 10, 8);
    l->setSpacing(4);
    QString tag = user ? QStringLiteral("你") : QStringLiteral("模");
    if (!turn.model.isEmpty())
        tag += QStringLiteral(" · ") + turn.model;
    auto *head = muted(tag, card, true);
    head->setObjectName(QStringLiteral("head"));
    l->addWidget(head);
    auto *body = new QLabel(turn.content, card);
    body->setObjectName(QStringLiteral("body"));
    body->setWordWrap(true);
    body->setTextInteractionFlags(Qt::TextSelectableByMouse);
    l->addWidget(body);
    if (turn.failed) {
        auto *fail = new QLabel(QStringLiteral("回覆失敗"), card);
        fail->setObjectName(QStringLiteral("failTag"));
        fail->setStyleSheet(QStringLiteral("color:#dc6060;font-size:11px"));
        l->addWidget(fail);
    }
    return card;
}

void StarChatWindow::updateCard(int index) {
    if (index < 0 || index >= turns_.size() || index >= cards_.size())
        return;
    const Turn &turn = turns_[index];
    QWidget *card = cards_[index];
    QString tag = turn.role == QLatin1String("user")
        ? QStringLiteral("你") : QStringLiteral("模");
    if (!turn.model.isEmpty())
        tag += QStringLiteral(" · ") + turn.model;
    card->findChild<QLabel *>(QStringLiteral("head"))->setText(tag);
    card->findChild<QLabel *>(QStringLiteral("body"))
        ->setText(turn.content);
    card->setProperty("card", turn.failed
        ? QStringLiteral("failed")
        : turn.role == QLatin1String("user")
            ? QStringLiteral("user") : QStringLiteral("assistant"));
    card->style()->unpolish(card);
    card->style()->polish(card);
    if (turn.failed &&
        !card->findChild<QLabel *>(QStringLiteral("failTag"))) {
        auto *fail = new QLabel(QStringLiteral("回覆失敗"), card);
        fail->setObjectName(QStringLiteral("failTag"));
        fail->setStyleSheet(QStringLiteral("color:#dc6060;font-size:11px"));
        card->layout()->addWidget(fail);
    }
}

void StarChatWindow::clearTurns() {
    turns_.clear();
    for (QWidget *card : cards_)
        card->deleteLater();
    cards_.clear();
    emptyState_->setVisible(true);
}

void StarChatWindow::scrollToEnd() {
    QTimer::singleShot(0, this, [this] {
        chatScroll_->verticalScrollBar()->setValue(
            chatScroll_->verticalScrollBar()->maximum());
    });
}

// ------------------------------------------------------------ actions --

QString StarChatWindow::sendRequest(const QString &command,
                                    const QJsonObject &payload) {
    const QString requestId = QStringLiteral("star-chat-%1-%2")
        .arg(++requestSeq_)
        .arg(QCoreApplication::applicationPid());
    QJsonObject body = payload;
    body.insert(QStringLiteral("request_id"), requestId);
    socket_->sendCommand(command, body);
    return requestId;
}

void StarChatWindow::sendMessage() {
    const QString message = draftEdit_->text().trimmed();
    if (message.isEmpty() || generating_ || !socket_->isOpen())
        return;
    appendTurn(QStringLiteral("user"), message);
    appendTurn(QStringLiteral("assistant"),
               QStringLiteral("正在準備回覆…"));
    const int assistantIndex = turns_.size() - 1;

    // egui parity: last 8 turns preceding the fresh assistant slot,
    // empty/failed turns dropped, chronological order.
    QJsonArray history;
    const int start = qMax(0, turns_.size() - 1 - kHistoryTurns);
    for (int i = start; i < turns_.size() - 1; ++i) {
        const Turn &t = turns_[i];
        if (t.content.isEmpty() || t.failed)
            continue;
        history.append(QJsonObject{
            {QStringLiteral("role"), t.role},
            {QStringLiteral("content"), t.content},
        });
    }
    const QString requestId = sendRequest(
        QStringLiteral("star_chat_send_message"),
        QJsonObject{
            {QStringLiteral("message"), message},
            {QStringLiteral("history"), history},
            {QStringLiteral("max_output_tokens"), 512},
            {QStringLiteral("runtime_model"), selectedModel_},
            {QStringLiteral("context_budget_characters"), 10000},
            {QStringLiteral("autonomous_agent"), true},
            {QStringLiteral("programming_folder"), QString()},
        });
    pending_ = Pending{requestId,
                       QStringLiteral("star_chat_send_message"),
                       false, assistantIndex};
    hasPending_ = true;
    draftEdit_->clear();
    generating_ = true;
    stage_ = QStringLiteral("準備處理");
    thinkingSince_ = QDateTime::currentDateTimeUtc();
    refreshComposer();
    scrollToEnd();
}

void StarChatWindow::runDiagnostic(const QString &command,
                                   const QString &label) {
    if (generating_ || !socket_->isOpen())
        return;
    appendTurn(QStringLiteral("user"), label);
    appendTurn(QStringLiteral("assistant"), QStringLiteral("檢查中…"));
    const int assistantIndex = turns_.size() - 1;
    const QString requestId = sendRequest(command, QJsonObject{});
    pending_ = Pending{requestId, command, false, assistantIndex};
    hasPending_ = true;
    generating_ = true;
    stage_ = QStringLiteral("法典檢查");
    thinkingSince_ = QDateTime::currentDateTimeUtc();
    refreshComposer();
    scrollToEnd();
}

void StarChatWindow::stopGenerating() {
    if (hasPending_) {
        if (pending_.command == QLatin1String("star_chat_send_message")) {
            socket_->sendCommand(
                QStringLiteral("toolbox_cancel_tool_run"),
                QJsonObject{{QStringLiteral("request_id"),
                             pending_.requestId}});
        }
        if (pending_.assistantIndex >= 0 &&
            pending_.assistantIndex < turns_.size()) {
            turns_[pending_.assistantIndex].content =
                QStringLiteral("已停止產生回答。");
            updateCard(pending_.assistantIndex);
        }
        hasPending_ = false;
    }
    generating_ = false;
    refreshComposer();
}

// ------------------------------------------------------------ events --

void StarChatWindow::onSocketConnected(bool connected) {
    if (connected) {
        sendRequest(QStringLiteral("star_chat_status"), QJsonObject{});
    } else if (hasPending_) {
        // Socket dropped mid-request — egui parity: the pending turn is
        // marked as a broken connection, not a model failure.
        turns_[pending_.assistantIndex].content =
            QStringLiteral("後端連線中斷，請重新送出訊息。");
        turns_[pending_.assistantIndex].failed = true;
        updateCard(pending_.assistantIndex);
        hasPending_ = false;
        generating_ = false;
    }
    refreshComposer();
}

void StarChatWindow::pollStatus() {
    if (socket_->isOpen())
        sendRequest(QStringLiteral("star_chat_status"), QJsonObject{});
}

void StarChatWindow::onFrame(const QString &event,
                             const QJsonObject &payload) {
    const QString requestId =
        payload.value(QStringLiteral("request_id")).toString();
    const bool matchesPending =
        hasPending_ && pending_.requestId == requestId;

    if (event == QLatin1String("error") &&
        (matchesPending || hasPending_)) {
        const QString message =
            payload.value(QStringLiteral("message")).toString();
        failPending(message.isEmpty()
                        ? QStringLiteral("後端處理失敗。") : message);
        return;
    }
    if (event == QLatin1String("star_chat_status_result")) {
        applyStatus(payload);
        return;
    }
    if (event.endsWith(QLatin1String("_progress")) && matchesPending) {
        handleProgress(payload);
        return;
    }
    if (event.endsWith(QLatin1String("_result")) && matchesPending) {
        finishPending(payload);
    }
}

void StarChatWindow::handleProgress(const QJsonObject &payload) {
    const QString phase =
        payload.value(QStringLiteral("phase")).toString();
    static const QHash<QString, QString> stageMap{
        {QStringLiteral("command-understanding"),
         QStringLiteral("繁中命令理解")},
        {QStringLiteral("workflow-frontend"), QStringLiteral("流程規劃")},
        {QStringLiteral("command-planned"), QStringLiteral("專家模型處理")},
        {QStringLiteral("generating"), QStringLiteral("生成回答")},
    };
    stage_ = stageMap.value(
        phase,
        payload.value(QStringLiteral("message"))
            .toString(QStringLiteral("處理中")));
    const QString text =
        payload.value(QStringLiteral("text")).toString();
    if (!text.isEmpty()) {
        pending_.streamed = true;
        if (pending_.assistantIndex >= 0 &&
            pending_.assistantIndex < turns_.size()) {
            turns_[pending_.assistantIndex].content = text;
            turns_[pending_.assistantIndex].model =
                payload.value(QStringLiteral("model")).toString();
            updateCard(pending_.assistantIndex);
        }
        scrollToEnd();
    }
}

void StarChatWindow::finishPending(const QJsonObject &payload) {
    if (!hasPending_)
        return;
    const bool ok = !payload.contains(QStringLiteral("ok")) ||
                    payload.value(QStringLiteral("ok")).toBool();
    QString text =
        payload.value(QStringLiteral("response")).toString().trimmed();
    if (text.isEmpty())
        text = payload.value(QStringLiteral("message")).toString().trimmed();
    if (text.isEmpty())
        text = ok ? QStringLiteral("模型已完成處理。")
                  : QStringLiteral("所選模型目前無法完成這項要求。");
    QString model = payload.value(QStringLiteral("generation"))
                        .toObject()
                        .value(QStringLiteral("model"))
                        .toString();
    if (model.isEmpty())
        model = payload.value(QStringLiteral("model")).toString();
    if (pending_.assistantIndex >= 0 &&
        pending_.assistantIndex < turns_.size()) {
        turns_[pending_.assistantIndex].content = text;
        turns_[pending_.assistantIndex].failed = !ok;
        turns_[pending_.assistantIndex].model = model;
        updateCard(pending_.assistantIndex);
    }
    hasPending_ = false;
    generating_ = false;
    refreshComposer();
    scrollToEnd();
}

void StarChatWindow::failPending(const QString &message) {
    if (hasPending_ && pending_.assistantIndex >= 0 &&
        pending_.assistantIndex < turns_.size()) {
        turns_[pending_.assistantIndex].content = message;
        turns_[pending_.assistantIndex].failed = true;
        updateCard(pending_.assistantIndex);
    }
    hasPending_ = false;
    generating_ = false;
    refreshComposer();
}

void StarChatWindow::applyStatus(const QJsonObject &payload) {
    const QJsonArray items = payload.value(QStringLiteral("native_runtime"))
                                 .toObject()
                                 .value(QStringLiteral("selectable_models"))
                                 .toArray();
    QVector<QPair<QString, QString>> options{
        {QString(),
         QStringLiteral("自動模型路由（速度、推理與能力強度）")}};
    for (const QJsonValue &item : items) {
        const QJsonObject entry = item.toObject();
        if (entry.value(QStringLiteral("installed")).toBool() != true)
            continue;
        const QString name =
            entry.value(QStringLiteral("name")).toString();
        if (name.isEmpty())
            continue;
        options.append({name, entry.value(QStringLiteral("label"))
                                  .toString(name)});
    }
    if (options.size() <= 1)
        return;  // parity: keep prior model list when catalog is empty
    models_ = options;
    const QString previous = selectedModel_;
    modelPicker_->clear();
    for (const auto &m : models_)
        modelPicker_->addItem(m.second, m.first);
    const int idx = modelPicker_->findData(previous);
    modelPicker_->setCurrentIndex(idx >= 0 ? idx : 0);
    selectedModel_ = modelPicker_->currentData().toString();
    modelPicker_->setVisible(true);
}

// -------------------------------------------------------------- misc --

void StarChatWindow::refreshComposer() {
    draftEdit_->setEnabled(!generating_);
    sendBtn_->setVisible(!generating_);
    stopBtn_->setVisible(generating_);
    sendBtn_->setEnabled(!generating_ && socket_->isOpen());
    diagAlignBtn_->setEnabled(!generating_ && socket_->isOpen());
    diagSyncBtn_->setEnabled(!generating_ && socket_->isOpen());
    stageLabel_->setVisible(generating_);
    refreshStage();
}

void StarChatWindow::refreshStage() {
    if (!generating_)
        return;
    const qint64 elapsed = thinkingSince_.isValid()
        ? thinkingSince_.secsTo(QDateTime::currentDateTimeUtc())
        : 0;
    stageLabel_->setText(
        QStringLiteral("%1 · 已處理 %2 秒").arg(stage_).arg(elapsed));
}
