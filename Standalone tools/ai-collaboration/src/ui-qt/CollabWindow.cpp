#include "CollabWindow.h"

#include "BackendSocket.h"
#include "BrowserOpsHandler.h"

#include <QApplication>
#include <QButtonGroup>
#include <QCheckBox>
#include <QClipboard>
#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFormLayout>
#include <QFrame>
#include <QGuiApplication>
#include <QHBoxLayout>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QPlainTextEdit>
#include <QProgressBar>
#include <QPushButton>
#include <QScrollArea>
#include <QSplitter>
#include <QStackedWidget>
#include <QTimer>
#include <QToolButton>
#include <QUuid>
#include <QVBoxLayout>
#include <QWebEngineView>

namespace {

// Provider brand palette — parity with the retired web surface.
QColor providerColor(const QString &provider) {
    static const QHash<QString, QString> colors{
        {QStringLiteral("chatgpt"), QStringLiteral("#10a37f")},
        {QStringLiteral("claude"), QStringLiteral("#d97757")},
        {QStringLiteral("gemini"), QStringLiteral("#4285f4")},
        {QStringLiteral("grok"), QStringLiteral("#6e7681")},
        {QStringLiteral("deepseek"), QStringLiteral("#4d6bfe")},
        {QStringLiteral("perplexity"), QStringLiteral("#20b8cd")},
    };
    return QColor(colors.value(provider, QStringLiteral("#8b949e")));
}

QString providerMonogram(const QString &provider) {
    static const QHash<QString, QString> marks{
        {QStringLiteral("chatgpt"), QStringLiteral("CG")},
        {QStringLiteral("claude"), QStringLiteral("CL")},
        {QStringLiteral("gemini"), QStringLiteral("GM")},
        {QStringLiteral("grok"), QStringLiteral("GK")},
        {QStringLiteral("deepseek"), QStringLiteral("DS")},
        {QStringLiteral("perplexity"), QStringLiteral("PX")},
    };
    return marks.value(provider, QStringLiteral("AI"));
}

QColor statusColor(const QString &status) {
    if (status == QLatin1String("completed"))
        return QColor(QStringLiteral("#3fb950"));
    if (status == QLatin1String("running") ||
        status == QLatin1String("opened") ||
        status == QLatin1String("authorizing"))
        return QColor(QStringLiteral("#58a6ff"));
    if (status == QLatin1String("awaiting-user") ||
        status == QLatin1String("waiting_verification") ||
        status == QLatin1String("waiting"))
        return QColor(QStringLiteral("#d29922"));
    if (status == QLatin1String("failed") || status == QLatin1String("cancelled"))
        return QColor(QStringLiteral("#f85149"));
    return QColor(QStringLiteral("#8b949e"));
}

QString statusLabel(const QString &status) {
    static const QHash<QString, QString> labels{
        {QStringLiteral("idle"), QStringLiteral("待命")},
        {QStringLiteral("opened"), QStringLiteral("已開啟")},
        {QStringLiteral("authorizing"), QStringLiteral("授權中")},
        {QStringLiteral("running"), QStringLiteral("執行中")},
        {QStringLiteral("completed"), QStringLiteral("完成")},
        {QStringLiteral("awaiting-user"), QStringLiteral("等待匯入")},
        {QStringLiteral("waiting_verification"), QStringLiteral("等待驗證")},
        {QStringLiteral("waiting"), QStringLiteral("等待中")},
        {QStringLiteral("failed"), QStringLiteral("失敗")},
        {QStringLiteral("cancelled"), QStringLiteral("已取消")},
    };
    return labels.value(status, status);
}

QString jstr(const QJsonObject &o, const char *key) {
    return o.value(QLatin1String(key)).toString();
}

QJsonArray jarr(const QJsonObject &o, const char *key) {
    return o.value(QLatin1String(key)).toArray();
}

QString newId() {
    return QUuid::createUuid().toString(QUuid::WithoutBraces).left(12);
}

QLabel *makeLabel(const QString &text, const QString &cls, QWidget *parent) {
    auto *l = new QLabel(text, parent);
    l->setProperty("class", cls);
    return l;
}

void clearLayout(QLayout *layout) {
    while (QLayoutItem *item = layout->takeAt(0)) {
        if (QWidget *w = item->widget())
            w->deleteLater();
        delete item;
    }
}

const char *kStyle = R"(
QMainWindow, QDialog { background: #0b0e14; }
QWidget { color: #c9d1d9; font-family: "Microsoft JhengHei UI", "Segoe UI"; font-size: 13px; }
QLabel { background: transparent; }
QLabel[class="title"] { font-size: 16px; font-weight: 700; color: #f0f6fc; }
QLabel[class="subtitle"] { color: #8b949e; }
QLabel[class="section"] { font-weight: 600; color: #f0f6fc; }
QLabel[class="conn"] { padding: 3px 10px; border-radius: 9px; background: #21262d; color: #8b949e; }
QLabel[class="conn-ok"] { padding: 3px 10px; border-radius: 9px; background: #11261b; color: #3fb950; border: 1px solid #1f6f43; }
QLabel[class="error"] { color: #f85149; padding: 4px 0; }
QLabel[class="muted"] { color: #8b949e; }
QLabel[class="mono"] { font-family: Consolas; color: #7d8590; }
QFrame[class="card"], QFrame[class="agent-card"] {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #141a24, stop:1 #10141c);
    border: 1px solid #232a36; border-radius: 10px;
}
QFrame[class="agent-card"]:hover { border-color: #3a4555; }
QFrame[class="agent-card"][selected="true"] { border-color: #4fd1c5; }
QLabel[class="monogram"] {
    border-radius: 8px; font-weight: 700; color: white;
    padding: 6px 8px; min-width: 26px; min-height: 26px;
    font-size: 12px; qproperty-alignment: AlignCenter;
}
QLabel[class="pill"] { padding: 2px 8px; border-radius: 8px; font-size: 11px; }
QPushButton {
    background: #21262d; border: 1px solid #30363d; border-radius: 8px;
    padding: 6px 14px; color: #c9d1d9;
}
QPushButton:hover { background: #2d333b; border-color: #4a5158; }
QPushButton:disabled { color: #6e7681; background: #161b22; }
QPushButton[class="primary"] {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #0e7a6c, stop:1 #0d9488);
    border-color: #0d9488; color: #f0fdfa; font-weight: 600;
}
QPushButton[class="primary"]:hover { background: #14a08f; }
QPushButton[class="danger"] { border-color: #8e3a35; color: #f85149; }
QPushButton[class="mini"] { padding: 2px 10px; font-size: 11px; border-radius: 6px; }
QPushButton[class="chip"] {
    border: 1px dashed #3a4555; border-radius: 10px; padding: 3px 10px;
    color: #8b949e; background: transparent; font-size: 11px;
}
QPushButton[class="chip"]:hover { border-style: solid; color: #4fd1c5; border-color: #4fd1c5; }
QPushButton[class="seg"] { border-radius: 0; padding: 5px 12px; }
QPushButton[class="seg"]:checked {
    background: #164e46; color: #5eead4; border-color: #0d9488;
}
QToolButton { background: transparent; border: none; padding: 4px 8px; border-radius: 6px; }
QToolButton:hover { background: #21262d; }
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox {
    background: #0d1117; border: 1px solid #30363d; border-radius: 8px;
    padding: 6px 8px; selection-background-color: #1f6feb;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus { border-color: #4fd1c5; }
QComboBox QAbstractItemView { background: #161b22; border: 1px solid #30363d; }
QProgressBar { border: none; background: #21262d; max-height: 3px; }
QProgressBar::chunk { background: #4fd1c5; }
QScrollBar:vertical { background: transparent; width: 8px; }
QScrollBar::handle:vertical { background: #30363d; border-radius: 4px; min-height: 24px; }
QScrollBar:horizontal { background: transparent; height: 8px; }
QScrollBar::handle:horizontal { background: #30363d; border-radius: 4px; min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QSplitter::handle { background: #1c2128; }
QListWidget { background: #0d1117; border: 1px solid #232a36; border-radius: 8px; }
QCheckBox { spacing: 6px; }
QToolTip { background: #161b22; color: #c9d1d9; border: 1px solid #30363d; }
)";

} // namespace

CollabWindow::CollabWindow(const QString &toolId, const QString &wsUrl,
                           QWidget *parent)
    : QMainWindow(parent), toolId_(toolId), wsUrl_(wsUrl) {
    setStyleSheet(QString::fromUtf8(kStyle));
    socket_ = new BackendSocket(this);

    auto *central = new QWidget(this);
    auto *root = new QVBoxLayout(central);
    root->setContentsMargins(14, 10, 14, 10);
    root->setSpacing(10);
    root->addWidget(buildHeader());
    root->addWidget(buildRail());

    auto *split = new QSplitter(Qt::Horizontal, central);
    auto *left = new QWidget;
    auto *leftBox = new QVBoxLayout(left);
    leftBox->setContentsMargins(0, 0, 0, 0);
    leftBox->setSpacing(10);
    leftBox->addWidget(buildComposer());
    leftBox->addWidget(buildFeed(), 1);
    split->addWidget(left);
    split->addWidget(buildBrowserPane());
    split->setStretchFactor(0, 1);
    split->setStretchFactor(1, 1);
    split->setSizes({560, 720});
    root->addWidget(split, 1);
    setCentralWidget(central);

    connect(socket_, &BackendSocket::statusChanged, this, [this](const QString &s) {
        if (s == QLatin1String("Connected")) {
            connPill_->setText(QStringLiteral("● 已連線"));
            connPill_->setProperty("class", "conn-ok");
        } else {
            connPill_->setText(QStringLiteral("○ ") + s);
            connPill_->setProperty("class", "conn");
        }
        connPill_->style()->unpolish(connPill_);
        connPill_->style()->polish(connPill_);
    });
    connect(socket_, &BackendSocket::frameReceived, this,
            &CollabWindow::onFrame);
    connect(socket_, &BackendSocket::socketConnected, this,
            &CollabWindow::onSocketConnected);

    resyncTimer_ = new QTimer(this);
    resyncTimer_->setInterval(5000);
    connect(resyncTimer_, &QTimer::timeout, this, &CollabWindow::resync);
    resyncTimer_->start();

    socket_->start(QUrl(wsUrl_));
}

// ---------------- layout ----------------

QWidget *CollabWindow::buildHeader() {
    auto *bar = new QWidget(this);
    auto *h = new QHBoxLayout(bar);
    h->setContentsMargins(0, 0, 0, 0);
    auto *title = makeLabel(QStringLiteral("◆ AI 協作"), "title", bar);
    auto *sub = makeLabel(QStringLiteral("最多六家外部 AI 協同"), "subtitle", bar);
    connPill_ = makeLabel(QStringLiteral("○ 未連線"), "conn", bar);
    auto *refresh = new QPushButton(QStringLiteral("重整"), bar);
    auto *report = new QPushButton(QStringLiteral("匯出報告"), bar);
    auto *settings = new QPushButton(QStringLiteral("設定"), bar);
    connect(refresh, &QPushButton::clicked, this, &CollabWindow::resync);
    connect(report, &QPushButton::clicked, this, &CollabWindow::exportReport);
    connect(settings, &QPushButton::clicked, this, &CollabWindow::showSettings);
    h->addWidget(title);
    h->addWidget(sub);
    h->addStretch();
    h->addWidget(connPill_);
    h->addWidget(refresh);
    h->addWidget(report);
    h->addWidget(settings);
    return bar;
}

QWidget *CollabWindow::buildRail() {
    auto *scroll = new QScrollArea(this);
    scroll->setWidgetResizable(true);
    scroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAsNeeded);
    scroll->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    scroll->setFixedHeight(104);
    scroll->setFrameShape(QFrame::NoFrame);
    railContent_ = new QWidget;
    auto *h = new QHBoxLayout(railContent_);
    h->setContentsMargins(2, 2, 2, 2);
    h->setSpacing(8);
    h->addStretch();
    scroll->setWidget(railContent_);
    return scroll;
}

QWidget *CollabWindow::buildComposer() {
    auto *card = new QFrame(this);
    card->setProperty("class", "card");
    auto *v = new QVBoxLayout(card);
    v->setContentsMargins(12, 10, 12, 10);
    v->setSpacing(8);

    auto *modeRow = new QHBoxLayout;
    modeGroup_ = new QButtonGroup(card);
    const QPair<QString, QString> modes[] = {
        {QStringLiteral("single"), QStringLiteral("單一 AI")},
        {QStringLiteral("compare"), QStringLiteral("多 AI 比較")},
        {QStringLiteral("sequential_review"), QStringLiteral("依序審查")},
    };
    for (int i = 0; i < 3; ++i) {
        auto *b = new QPushButton(modes[i].second, card);
        b->setCheckable(true);
        b->setProperty("class", "seg");
        b->setChecked(i == 0);
        modeGroup_->addButton(b, i);
        modeRow->addWidget(b);
    }
    modeRow->addSpacing(8);
    modeRow->addWidget(makeLabel(QStringLiteral("AI："), "muted", card));
    singleProvider_ = new QComboBox(card);
    singleProvider_->setMinimumWidth(150);
    modeRow->addWidget(singleProvider_);
    modeRow->addStretch();
    v->addLayout(modeRow);

    auto *chips = new QHBoxLayout;
    const QPair<QString, QString> tpls[] = {
        {QStringLiteral("摘要"), QStringLiteral("summary")},
        {QStringLiteral("比較"), QStringLiteral("compare")},
        {QStringLiteral("反證"), QStringLiteral("counter")},
        {QStringLiteral("行動"), QStringLiteral("action")},
    };
    for (const auto &t : tpls) {
        auto *chip = new QPushButton(t.first, card);
        chip->setProperty("class", "chip");
        const QString kind = t.second;
        connect(chip, &QPushButton::clicked, this,
                [this, kind] { applyTemplate(kind); });
        chips->addWidget(chip);
    }
    chips->addStretch();
    v->addLayout(chips);

    promptEdit_ = new QPlainTextEdit(card);
    promptEdit_->setPlaceholderText(
        QStringLiteral("輸入要交給 AI 協作的內容…"));
    promptEdit_->setMinimumHeight(72);
    promptEdit_->setMaximumHeight(140);
    v->addWidget(promptEdit_);

    auto *actions = new QHBoxLayout;
    statusLine_ = makeLabel(QStringLiteral(""), "muted", card);
    sendBtn_ = new QPushButton(QStringLiteral("送出協作"), card);
    sendBtn_->setProperty("class", "primary");
    cancelBtn_ = new QPushButton(QStringLiteral("取消任務"), card);
    cancelBtn_->setProperty("class", "danger");
    cancelBtn_->setVisible(false);
    connect(sendBtn_, &QPushButton::clicked, this, &CollabWindow::send);
    connect(cancelBtn_, &QPushButton::clicked, this,
            [this] { cancelTask(currentTaskId_); });
    actions->addWidget(statusLine_, 1);
    actions->addWidget(cancelBtn_);
    actions->addWidget(sendBtn_);
    v->addLayout(actions);

    errorLabel_ = makeLabel(QStringLiteral(""), "error", card);
    errorLabel_->setVisible(false);
    errorLabel_->setWordWrap(true);
    v->addWidget(errorLabel_);
    return card;
}

QWidget *CollabWindow::buildFeed() {
    auto *card = new QFrame(this);
    card->setProperty("class", "card");
    auto *v = new QVBoxLayout(card);
    v->setContentsMargins(12, 10, 12, 10);
    auto *head = makeLabel(QStringLiteral("協作結果"), "section", card);
    v->addWidget(head);
    taskSlot_ = new QWidget(card);
    auto *taskBox = new QVBoxLayout(taskSlot_);
    taskBox->setContentsMargins(0, 0, 0, 0);
    v->addWidget(taskSlot_);
    auto *scroll = new QScrollArea(card);
    scroll->setWidgetResizable(true);
    scroll->setFrameShape(QFrame::NoFrame);
    auto *body = new QWidget;
    feedLayout_ = new QVBoxLayout(body);
    feedLayout_->setContentsMargins(0, 0, 4, 0);
    feedLayout_->setSpacing(8);
    feedLayout_->addStretch();
    scroll->setWidget(body);
    v->addWidget(scroll, 1);
    return card;
}

QWidget *CollabWindow::buildBrowserPane() {
    auto *card = new QFrame(this);
    card->setProperty("class", "card");
    auto *v = new QVBoxLayout(card);
    v->setContentsMargins(10, 8, 10, 8);
    v->setSpacing(6);

    auto *nav = new QHBoxLayout;
    auto *back = new QToolButton(card);
    back->setText(QStringLiteral("◀"));
    auto *fwd = new QToolButton(card);
    fwd->setText(QStringLiteral("▶"));
    auto *reload = new QToolButton(card);
    reload->setText(QStringLiteral("⟳"));
    urlEdit_ = new QLineEdit(card);
    urlEdit_->setPlaceholderText(QStringLiteral("https://…"));
    auto *go = new QPushButton(QStringLiteral("前往"), card);
    go->setProperty("class", "mini");
    sessionPicker_ = new QComboBox(card);
    sessionPicker_->setMinimumWidth(140);
    sessionPicker_->addItem(QStringLiteral("（無分頁）"), QString());
    nav->addWidget(back);
    nav->addWidget(fwd);
    nav->addWidget(reload);
    nav->addWidget(urlEdit_, 1);
    nav->addWidget(go);
    nav->addWidget(sessionPicker_);
    v->addLayout(nav);

    loadBar_ = new QProgressBar(card);
    loadBar_->setRange(0, 100);
    loadBar_->setValue(0);
    loadBar_->setTextVisible(false);
    v->addWidget(loadBar_);

    browserStack_ = new QStackedWidget(card);
    auto *placeholder = new QLabel(
        QStringLiteral("◇\n\n外部 AI 分頁會顯示在這裡\n選取 AI 後按「開啟」建立分頁"), card);
    placeholder->setAlignment(Qt::AlignCenter);
    placeholder->setProperty("class", "muted");
    browserStack_->addWidget(placeholder);
    v->addWidget(browserStack_, 1);

    browserOps_ = new BrowserOpsHandler(browserStack_, placeholder, socket_,
                                        this);

    connect(back, &QToolButton::clicked, this, [this] {
        if (auto *v = browserOps_->activeView())
            v->back();
    });
    connect(fwd, &QToolButton::clicked, this, [this] {
        if (auto *v = browserOps_->activeView())
            v->forward();
    });
    connect(reload, &QToolButton::clicked, this, [this] {
        if (auto *v = browserOps_->activeView())
            v->reload();
    });
    connect(go, &QPushButton::clicked, this, [this] {
        if (auto *v = browserOps_->activeView()) {
            const QString u = urlEdit_->text().trimmed();
            if (u.startsWith(QLatin1String("https://")))
                v->load(QUrl(u));
        }
    });
    connect(urlEdit_, &QLineEdit::returnPressed, this, [this] {
        if (auto *v = browserOps_->activeView()) {
            const QString u = urlEdit_->text().trimmed();
            if (u.startsWith(QLatin1String("https://")))
                v->load(QUrl(u));
        }
    });
    connect(sessionPicker_, &QComboBox::activated, this, [this](int idx) {
        const QString sid = sessionPicker_->itemData(idx).toString();
        if (sid.isEmpty())
            browserOps_->showPlaceholder();
        else
            browserOps_->activateSession(sid);
    });
    connect(browserOps_, &BrowserOpsHandler::activeSessionUrlChanged, this,
            [this](const QString &, const QString &url) {
                urlEdit_->setText(url);
            });
    connect(browserOps_, &BrowserOpsHandler::loadProgressChanged,
            loadBar_, &QProgressBar::setValue);
    connect(browserOps_, &BrowserOpsHandler::loadFinishedChanged,
            loadBar_, [this](bool) { loadBar_->setValue(100); });
    return card;
}

// ---------------- frames ----------------

void CollabWindow::onSocketConnected(bool connected) {
    if (connected)
        resync();
}

void CollabWindow::resync() {
    socket_->sendCommand(QStringLiteral("ai_nexus_get_state"), {});
}

void CollabWindow::onFrame(const QString &event, const QJsonObject &payload) {
    if (event == QLatin1String("ai_collab_browser_op")) {
        browserOps_->handleOp(payload);
        return;
    }
    if (!event.endsWith(QLatin1String("_result")))
        return;

    applyState(payload);

    if (event == QLatin1String("ai_nexus_collab_start_result") ||
        event == QLatin1String("ai_nexus_collab_resume_result")) {
        const QString tid = jstr(payload, "task_id");
        if (!tid.isEmpty())
            currentTaskId_ = tid;
        setBusy(false);
    }
    if (event == QLatin1String("ai_nexus_send_message_result") ||
        event == QLatin1String("ai_nexus_collab_start_result")) {
        setBusy(false);
    }
    if (event == QLatin1String("ai_nexus_export_report_result")) {
        const QString path = jstr(payload, "report_path");
        if (!path.isEmpty())
            flash(QStringLiteral("報告已匯出：") + path);
    }

    const bool ok = payload.value(QStringLiteral("ok")).toBool();
    const QString msg = jstr(payload, "message");
    if (!ok && !msg.isEmpty())
        setError(msg);
    else if (ok && !msg.isEmpty() && event != QLatin1String("ai_nexus_get_state_result"))
        flash(msg);

    renderAgents();
    renderFeed();
}

void CollabWindow::applyState(const QJsonObject &p) {
    if (p.contains(QStringLiteral("agents")))
        agents_ = jarr(p, "agents");
    if (p.contains(QStringLiteral("messages")))
        messages_ = jarr(p, "messages");
    if (p.contains(QStringLiteral("collab_tasks")))
        collabTasks_ = jarr(p, "collab_tasks");
    if (p.contains(QStringLiteral("memory_items")))
        memoryItems_ = jarr(p, "memory_items");
    if (p.contains(QStringLiteral("provider_execution")))
        providerExecution_ =
            p.value(QStringLiteral("provider_execution")).toObject();
    if (p.contains(QStringLiteral("task"))) {
        const QJsonObject t = p.value(QStringLiteral("task")).toObject();
        const QString tid = jstr(t, "task_id");
        if (!tid.isEmpty())
            currentTaskId_ = tid;
    }

    // session picker sync
    const QString active = browserOps_ ? browserOps_->activeSessionId() : QString();
    if (sessionPicker_) {
        sessionPicker_->blockSignals(true);
        sessionPicker_->clear();
        sessionPicker_->addItem(QStringLiteral("（無分頁）"), QString());
        if (browserOps_) {
            int activeIdx = 0;
            const QStringList ids = browserOps_->sessionIds();
            for (int i = 0; i < ids.size(); ++i) {
                sessionPicker_->addItem(ids.at(i).mid(17), ids.at(i));
                if (ids.at(i) == active)
                    activeIdx = i + 1;
            }
            sessionPicker_->setCurrentIndex(activeIdx);
        }
        sessionPicker_->blockSignals(false);
    }
}

// ---------------- rendering ----------------

void CollabWindow::renderAgents() {
    auto *layout = qobject_cast<QHBoxLayout *>(railContent_->layout());
    clearLayout(layout);
    selectedAgents_.clear();

    int selected = 0;
    for (const auto &v : agents_) {
        const QJsonObject a = v.toObject();
        const QString agentId = jstr(a, "agent_id");
        const QString provider = jstr(a, "provider");
        const QString status = jstr(a, "status");
        const bool sel = a.value(QStringLiteral("selected")).toBool();
        if (sel)
            selectedAgents_.insert(agentId);

        auto *card = new QFrame(railContent_);
        card->setProperty("class", "agent-card");
        card->setProperty("selected", sel);
        card->setFixedHeight(88);
        card->setMinimumWidth(190);
        auto *cv = new QVBoxLayout(card);
        cv->setContentsMargins(10, 8, 10, 8);
        cv->setSpacing(4);

        auto *row1 = new QHBoxLayout;
        auto *check = new QCheckBox(card);
        check->setChecked(sel);
        auto *mark = makeLabel(providerMonogram(provider), "monogram", card);
        mark->setStyleSheet(QStringLiteral("background:%1;")
                                .arg(providerColor(provider).name()));
        auto *name = makeLabel(jstr(a, "name"), "section", card);
        auto *dot = makeLabel(QStringLiteral("●"), "mono", card);
        dot->setStyleSheet(
            QStringLiteral("color:%1;").arg(statusColor(status).name()));
        row1->addWidget(check);
        row1->addWidget(mark);
        row1->addWidget(name, 1);
        row1->addWidget(dot);
        cv->addLayout(row1);

        auto *row2 = new QHBoxLayout;
        auto *pill = makeLabel(statusLabel(status), "pill", card);
        pill->setStyleSheet(QStringLiteral("color:%1; border:1px solid %1;")
                                .arg(statusColor(status).name()));
        row2->addWidget(pill);
        row2->addStretch();
        auto *open = new QPushButton(QStringLiteral("開啟"), card);
        open->setProperty("class", "mini");
        auto *auth = new QPushButton(QStringLiteral("登入"), card);
        auth->setProperty("class", "mini");
        connect(open, &QPushButton::clicked, this,
                [this, agentId] { openAgent(agentId); });
        connect(auth, &QPushButton::clicked, this,
                [this, agentId] { authorizeAgent(agentId); });
        row2->addWidget(open);
        row2->addWidget(auth);
        cv->addLayout(row2);

        connect(check, &QCheckBox::toggled, this, [this](bool) {
            QStringList ids;
            const auto boxes =
                railContent_->findChildren<QCheckBox *>();
            // Rebuild selection from live agents order.
            int idx = 0;
            for (const auto &av : agents_) {
                const QString id =
                    jstr(av.toObject(), "agent_id");
                if (idx < boxes.size() && boxes.at(idx)->isChecked())
                    ids << id;
                ++idx;
            }
            socket_->sendCommand(
                QStringLiteral("ai_nexus_set_agent_selection"),
                {{QStringLiteral("agent_ids"), QJsonArray::fromStringList(ids)}});
        });

        layout->addWidget(card);
        if (sel)
            ++selected;
    }
    layout->addStretch();

    // single-provider combo: selected agents
    const QString prev = singleProvider_->currentData().toString();
    singleProvider_->blockSignals(true);
    singleProvider_->clear();
    int restore = 0, i = 0;
    for (const auto &v : agents_) {
        const QJsonObject a = v.toObject();
        if (!a.value(QStringLiteral("selected")).toBool())
            continue;
        const QString id = jstr(a, "agent_id");
        singleProvider_->addItem(jstr(a, "name"), id);
        if (id == prev)
            restore = i;
        ++i;
    }
    if (singleProvider_->count() > 0)
        singleProvider_->setCurrentIndex(restore);
    singleProvider_->blockSignals(false);
}

void CollabWindow::renderFeed() {
    clearLayout(feedLayout_);
    // latest collab task card
    QJsonObject task;
    if (!currentTaskId_.isEmpty()) {
        for (const auto &v : collabTasks_) {
            const QJsonObject t = v.toObject();
            if (jstr(t, "task_id") == currentTaskId_)
                task = t;
        }
    }
    if (task.isEmpty() && !collabTasks_.isEmpty())
        task = collabTasks_.last().toObject();
    renderTask(task);
    cancelBtn_->setVisible(!currentTaskId_.isEmpty() && busy_);

    for (int i = messages_.size() - 1; i >= 0; --i)
        feedLayout_->addWidget(messageCard(messages_.at(i).toObject()));
    if (messages_.isEmpty()) {
        auto *empty = makeLabel(
            QStringLiteral("◇\n尚無協作紀錄\n選取 AI 並輸入內容開始協作"),
            "muted", nullptr);
        empty->setAlignment(Qt::AlignCenter);
        feedLayout_->addWidget(empty);
    }
    feedLayout_->addStretch();
}

void CollabWindow::renderTask(const QJsonObject &task) {
    auto *box = qobject_cast<QVBoxLayout *>(taskSlot_->layout());
    clearLayout(box);
    taskSlot_->setVisible(!task.isEmpty());
    if (task.isEmpty())
        return;
    if (jstr(task, "task_id") != currentTaskId_)
        currentTaskId_ = jstr(task, "task_id");

    const QString status = jstr(task, "overall_status");
    auto *card = new QFrame(taskSlot_);
    card->setProperty("class", "card");
    auto *v = new QVBoxLayout(card);
    v->setContentsMargins(10, 8, 10, 8);
    v->setSpacing(6);

    auto *head = new QHBoxLayout;
    head->addWidget(makeLabel(
        QStringLiteral("任務 %1").arg(jstr(task, "task_id").left(12)),
        "section", card));
    head->addStretch();
    auto *pill = makeLabel(statusLabel(status), "pill", card);
    pill->setStyleSheet(QStringLiteral("color:%1; border:1px solid %1;")
                            .arg(statusColor(status).name()));
    head->addWidget(pill);
    if (status == QLatin1String("waiting_user") ||
        status == QLatin1String("running")) {
        auto *resume = new QPushButton(QStringLiteral("繼續"), card);
        resume->setProperty("class", "mini");
        const QString tid = jstr(task, "task_id");
        connect(resume, &QPushButton::clicked, this, [this, tid] {
            socket_->sendCommand(QStringLiteral("ai_nexus_collab_resume"),
                                 {{QStringLiteral("task_id"), tid},
                                  {QStringLiteral("request_id"), newId()}});
        });
        head->addWidget(resume);
    }
    v->addLayout(head);

    for (const auto &rv : jarr(task, "provider_results")) {
        const QJsonObject r = rv.toObject();
        const QString provider = jstr(r, "provider_id");
        const QString rstatus = jstr(r, "response_status");
        auto *row = new QHBoxLayout;
        auto *mark = makeLabel(providerMonogram(provider), "monogram", card);
        mark->setStyleSheet(QStringLiteral("background:%1;")
                                .arg(providerColor(provider).name()));
        row->addWidget(mark);
        auto *rp = makeLabel(statusLabel(rstatus), "pill", card);
        rp->setStyleSheet(QStringLiteral("color:%1; border:1px solid %1;")
                              .arg(statusColor(rstatus).name()));
        row->addWidget(rp);
        row->addStretch();
        if (rstatus == QLatin1String("awaiting-user")) {
            auto *imp = new QPushButton(QStringLiteral("手動匯入"), card);
            imp->setProperty("class", "mini");
            const QString tid = jstr(task, "task_id");
            connect(imp, &QPushButton::clicked, this,
                    [this, tid, provider] { manualImport(tid, provider); });
            row->addWidget(imp);
        }
        auto *copy = new QPushButton(QStringLiteral("複製"), card);
        copy->setProperty("class", "mini");
        const QString text = jstr(r, "response_text");
        copy->setEnabled(!text.isEmpty());
        connect(copy, &QPushButton::clicked, this, [text] {
            QGuiApplication::clipboard()->setText(text);
        });
        row->addWidget(copy);
        v->addLayout(row);
        if (!text.isEmpty()) {
            auto *body = makeLabel(text, "muted", card);
            body->setWordWrap(true);
            body->setTextInteractionFlags(Qt::TextSelectableByMouse);
            v->addWidget(body);
        }
    }

    const QJsonObject comparison =
        task.value(QStringLiteral("comparison")).toObject();
    if (!comparison.isEmpty())
        v->addWidget(comparisonBlock(comparison));
    const QJsonObject synthesis =
        task.value(QStringLiteral("synthesis")).toObject();
    if (!synthesis.isEmpty())
        v->addWidget(synthesisBlock(synthesis));
    box->addWidget(card);
}

QWidget *CollabWindow::comparisonBlock(const QJsonObject &cmp) {
    auto *card = new QFrame(this);
    card->setProperty("class", "card");
    auto *v = new QVBoxLayout(card);
    v->setContentsMargins(8, 6, 8, 6);
    v->setSpacing(4);
    v->addWidget(makeLabel(QStringLiteral("比較結果"), "section", card));

    auto addRows = [&](const char *key, const QString &label,
                       const QString &mark) {
        const QJsonArray rows = jarr(cmp, key);
        if (rows.isEmpty())
            return;
        v->addWidget(makeLabel(label, "muted", card));
        int shown = 0;
        for (const auto &rv : rows) {
            if (shown++ >= 12)
                break;
            const QJsonObject r = rv.toObject();
            QString text = jstr(r, "text");
            if (text.isEmpty())
                text = jstr(r, "question");
            QString src = jstr(r, "source_provider");
            if (src.isEmpty())
                src = jstr(r, "raised_by");
            const QJsonArray sources = jarr(r, "sources");
            if (!sources.isEmpty()) {
                QStringList ss;
                for (const auto &s : sources)
                    ss << s.toString();
                src = ss.join(QLatin1String(", "));
            }
            v->addWidget(makeLabel(
                QStringLiteral("%1 [%2] %3").arg(mark, src, text),
                "muted", card));
        }
    };
    addRows("common_points", QStringLiteral("共同觀點"), QStringLiteral("+"));
    addRows("differences", QStringLiteral("各 AI 差異"), QStringLiteral("≠"));
    addRows("unanswered_questions", QStringLiteral("未回答的問題"),
            QStringLiteral("?"));

    const QJsonArray contradictions = jarr(cmp, "contradictions");
    if (!contradictions.isEmpty()) {
        v->addWidget(makeLabel(QStringLiteral("相互矛盾點"), "muted", card));
        for (const auto &cv : contradictions) {
            for (const auto &sv : jarr(cv.toObject(), "statements")) {
                const QJsonObject s = sv.toObject();
                v->addWidget(makeLabel(
                    QStringLiteral("× [%1] %2")
                        .arg(jstr(s, "provider_id"), jstr(s, "text")),
                    "muted", card));
            }
        }
    }
    const QString note = jstr(cmp, "note");
    if (!note.isEmpty())
        v->addWidget(makeLabel(note, "muted", card));
    return card;
}

QWidget *CollabWindow::synthesisBlock(const QJsonObject &syn) {
    auto *card = new QFrame(this);
    card->setProperty("class", "card");
    card->setStyleSheet(
        QStringLiteral("border-color: #0d9488;"));
    auto *v = new QVBoxLayout(card);
    v->setContentsMargins(8, 6, 8, 6);
    auto *head = new QHBoxLayout;
    head->addWidget(makeLabel(QStringLiteral("整合結果"), "section", card));
    head->addStretch();
    head->addWidget(makeLabel(jstr(syn, "method"), "muted", card));
    v->addLayout(head);
    auto *body = makeLabel(jstr(syn, "summary"), "muted", card);
    body->setWordWrap(true);
    body->setTextInteractionFlags(Qt::TextSelectableByMouse);
    v->addWidget(body);
    return card;
}

QWidget *CollabWindow::messageCard(const QJsonObject &message) {
    auto *card = new QFrame(this);
    card->setProperty("class", "card");
    auto *v = new QVBoxLayout(card);
    v->setContentsMargins(10, 8, 10, 8);
    v->setSpacing(6);
    auto *head = new QHBoxLayout;
    const QString scope = jstr(message, "business_scope");
    head->addWidget(makeLabel(
        QStringLiteral("#%1 %2")
            .arg(jstr(message, "message_id").left(8), scope),
        "mono", card));
    head->addStretch();
    head->addWidget(makeLabel(jstr(message, "created_at"), "muted", card));
    v->addLayout(head);
    auto *content = makeLabel(jstr(message, "content"), "muted", card);
    content->setWordWrap(true);
    v->addWidget(content);
    for (const auto &rv : jarr(message, "responses"))
        v->addWidget(responseRow(message, rv.toObject()));
    return card;
}

QWidget *CollabWindow::responseRow(const QJsonObject &message,
                                 const QJsonObject &r) {
    auto *row = new QFrame(this);
    auto *rv = new QVBoxLayout(row);
    rv->setContentsMargins(8, 4, 8, 4);
    rv->setSpacing(4);
    const QString agentId = jstr(r, "agent_id");
    const QString provider = jstr(r, "execution_provider").isEmpty()
                                 ? agentId
                                 : jstr(r, "execution_provider");
    const QString status = jstr(r, "status");
    auto *top = new QHBoxLayout;
    auto *mark = makeLabel(providerMonogram(provider), "monogram", row);
    mark->setStyleSheet(QStringLiteral("background:%1;")
                            .arg(providerColor(provider).name()));
    top->addWidget(mark);
    top->addWidget(makeLabel(agentId, "section", row));
    auto *pill = makeLabel(statusLabel(status), "pill", row);
    pill->setStyleSheet(QStringLiteral("color:%1; border:1px solid %1;")
                            .arg(statusColor(status).name()));
    top->addWidget(pill);
    top->addStretch();
    if (status == QLatin1String("awaiting-user") ||
        status == QLatin1String("waiting_verification")) {
        auto *imp = new QPushButton(QStringLiteral("手動匯入"), row);
        imp->setProperty("class", "mini");
        const QString mid = jstr(message, "message_id");
        connect(imp, &QPushButton::clicked, this,
                [this, mid, agentId] { completeBrowserResponse(mid, agentId); });
        top->addWidget(imp);
    }
    const QString text = jstr(r, "content");
    if (!text.isEmpty()) {
        auto *copy = new QPushButton(QStringLiteral("複製"), row);
        copy->setProperty("class", "mini");
        connect(copy, &QPushButton::clicked, this, [text] {
            QGuiApplication::clipboard()->setText(text);
        });
        top->addWidget(copy);
    }
    rv->addLayout(top);
    const QString err = jstr(r, "error");
    if (!text.isEmpty()) {
        auto *body = makeLabel(text, "muted", row);
        body->setWordWrap(true);
        body->setTextInteractionFlags(Qt::TextSelectableByMouse);
        rv->addWidget(body);
    } else if (!err.isEmpty()) {
        rv->addWidget(makeLabel(err, "error", row));
    }
    return row;
}

// ---------------- actions ----------------

QStringList CollabWindow::selectedProviderIds() const {
    QStringList out;
    for (const auto &v : agents_) {
        const QJsonObject a = v.toObject();
        if (a.value(QStringLiteral("selected")).toBool())
            out << jstr(a, "provider");
    }
    return out;
}

void CollabWindow::send() {
    const QString content = promptEdit_->toPlainText().trimmed();
    if (content.isEmpty()) {
        setError(QStringLiteral("請輸入要交給 AI 協作的內容"));
        return;
    }
    const int mode = modeGroup_->checkedId();
    static const char *modes[] = {"single", "compare", "sequential_review"};
    QStringList providers;
    if (mode == 0) {
        // single: use the combo-picked agent's provider
        const QString agentId = singleProvider_->currentData().toString();
        if (agentId.isEmpty()) {
            setError(QStringLiteral("請先選取一個 AI"));
            return;
        }
        for (const auto &v : agents_) {
            const QJsonObject a = v.toObject();
            if (jstr(a, "agent_id") == agentId)
                providers << jstr(a, "provider");
        }
    } else {
        providers = selectedProviderIds();
        if (providers.size() < 2) {
            setError(QStringLiteral("此協作模式至少需要兩個 AI"));
            return;
        }
    }
    setError(QString());
    setBusy(true);
    socket_->sendCommand(QStringLiteral("ai_nexus_collab_start"),
                         {{QStringLiteral("content"), content},
                          {QStringLiteral("mode"), QLatin1String(modes[mode])},
                          {QStringLiteral("provider_ids"),
                           QJsonArray::fromStringList(providers)},
                          {QStringLiteral("request_id"), newId()}});
}

void CollabWindow::cancelTask(const QString &taskId) {
    if (taskId.isEmpty())
        return;
    socket_->sendCommand(QStringLiteral("ai_nexus_collab_cancel"),
                         {{QStringLiteral("task_id"), taskId},
                          {QStringLiteral("request_id"), newId()}});
    setBusy(false);
}

void CollabWindow::manualImport(const QString &taskId, const QString &providerId) {
    QDialog dlg(this);
    dlg.setWindowTitle(QStringLiteral("手動匯入回覆 — %1").arg(providerId));
    auto *v = new QVBoxLayout(&dlg);
    auto *edit = new QPlainTextEdit(&dlg);
    edit->setPlaceholderText(QStringLiteral("貼上 AI 在瀏覽器中的回覆全文…"));
    edit->setMinimumSize(520, 320);
    v->addWidget(edit);
    auto *buttons = new QDialogButtonBox(
        QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dlg);
    v->addWidget(buttons);
    connect(buttons, &QDialogButtonBox::accepted, &dlg, &QDialog::accept);
    connect(buttons, &QDialogButtonBox::rejected, &dlg, &QDialog::reject);
    if (dlg.exec() != QDialog::Accepted)
        return;
    const QString text = edit->toPlainText().trimmed();
    if (text.isEmpty())
        return;
    socket_->sendCommand(QStringLiteral("ai_nexus_collab_manual_result"),
                         {{QStringLiteral("task_id"), taskId},
                          {QStringLiteral("provider_id"), providerId},
                          {QStringLiteral("content"), text},
                          {QStringLiteral("request_id"), newId()}});
}

void CollabWindow::completeBrowserResponse(const QString &messageId,
                                           const QString &agentId) {
    QDialog dlg(this);
    dlg.setWindowTitle(QStringLiteral("匯入瀏覽器回覆 — %1").arg(agentId));
    auto *v = new QVBoxLayout(&dlg);
    auto *edit = new QPlainTextEdit(&dlg);
    edit->setPlaceholderText(QStringLiteral("貼上該 AI 的回覆全文…"));
    edit->setMinimumSize(520, 320);
    v->addWidget(edit);
    auto *buttons = new QDialogButtonBox(
        QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dlg);
    v->addWidget(buttons);
    connect(buttons, &QDialogButtonBox::accepted, &dlg, &QDialog::accept);
    connect(buttons, &QDialogButtonBox::rejected, &dlg, &QDialog::reject);
    if (dlg.exec() != QDialog::Accepted)
        return;
    const QString text = edit->toPlainText().trimmed();
    if (text.isEmpty())
        return;
    socket_->sendCommand(
        QStringLiteral("ai_nexus_complete_browser_response"),
        {{QStringLiteral("message_id"), messageId},
         {QStringLiteral("agent_id"), agentId},
         {QStringLiteral("content"), text},
         {QStringLiteral("request_id"), newId()}});
}

void CollabWindow::openAgent(const QString &agentId) {
    socket_->sendCommand(QStringLiteral("ai_nexus_open_agent"),
                         {{QStringLiteral("agent_id"), agentId},
                          {QStringLiteral("request_id"), newId()}});
}

void CollabWindow::authorizeAgent(const QString &agentId) {
    socket_->sendCommand(QStringLiteral("ai_nexus_authorize_agent"),
                         {{QStringLiteral("agent_id"), agentId},
                          {QStringLiteral("request_id"), newId()}});
}

void CollabWindow::exportReport() {
    socket_->sendCommand(QStringLiteral("ai_nexus_export_report"),
                         {{QStringLiteral("request_id"), newId()}});
}

void CollabWindow::applyTemplate(const QString &kind) {
    static const QHash<QString, QString> tpl{
        {QStringLiteral("summary"), QStringLiteral("請摘要以下內容的重點：\n")},
        {QStringLiteral("compare"), QStringLiteral("請比較以下選項的優缺點：\n")},
        {QStringLiteral("counter"), QStringLiteral("請針對以下論點提出反證與風險：\n")},
        {QStringLiteral("action"), QStringLiteral("請為以下需求產出行動計畫：\n")},
    };
    promptEdit_->setPlainText(tpl.value(kind) + promptEdit_->toPlainText());
}

void CollabWindow::showSettings() {
    QDialog dlg(this);
    dlg.setWindowTitle(QStringLiteral("AI 協作設定"));
    auto *v = new QVBoxLayout(&dlg);

    // --- add agent ---
    v->addWidget(makeLabel(QStringLiteral("新增 AI"), "section", &dlg));
    auto *form = new QFormLayout;
    auto *name = new QLineEdit(&dlg);
    auto *provider = new QComboBox(&dlg);
    provider->addItems({QStringLiteral("chatgpt"), QStringLiteral("claude"),
                        QStringLiteral("gemini"), QStringLiteral("grok"),
                        QStringLiteral("deepseek"),
                        QStringLiteral("perplexity"),
                        QStringLiteral("custom")});
    auto *home = new QLineEdit(&dlg);
    home->setPlaceholderText(QStringLiteral("https://…"));
    form->addRow(QStringLiteral("名稱"), name);
    form->addRow(QStringLiteral("Provider"), provider);
    form->addRow(QStringLiteral("首頁 URL"), home);
    v->addLayout(form);
    auto *addBtn = new QPushButton(QStringLiteral("新增"), &dlg);
    connect(addBtn, &QPushButton::clicked, &dlg, [this, name, provider, home] {
        socket_->sendCommand(
            QStringLiteral("ai_nexus_add_agent"),
            {{QStringLiteral("name"), name->text().trimmed()},
             {QStringLiteral("provider"), provider->currentText()},
             {QStringLiteral("home_url"), home->text().trimmed()},
             {QStringLiteral("request_id"), newId()}});
    });
    v->addWidget(addBtn);

    // --- business settings for a chosen agent ---
    v->addWidget(makeLabel(QStringLiteral("業務範圍 URL"), "section", &dlg));
    auto *agentPick = new QComboBox(&dlg);
    for (const auto &av : agents_) {
        const QJsonObject a = av.toObject();
        agentPick->addItem(jstr(a, "name"), jstr(a, "agent_id"));
    }
    auto *bizForm = new QFormLayout;
    auto *generalUrl = new QLineEdit(&dlg);
    auto *investUrl = new QLineEdit(&dlg);
    auto *generalEn = new QCheckBox(QStringLiteral("啟用一般業務"), &dlg);
    generalEn->setChecked(true);
    auto *investEn = new QCheckBox(QStringLiteral("啟用投資業務"), &dlg);
    investEn->setChecked(true);
    bizForm->addRow(QStringLiteral("AI"), agentPick);
    bizForm->addRow(QStringLiteral("一般 URL"), generalUrl);
    bizForm->addRow(QStringLiteral("投資 URL"), investUrl);
    bizForm->addRow(generalEn);
    bizForm->addRow(investEn);
    v->addLayout(bizForm);
    auto populate = [this, agentPick, generalUrl, investUrl, generalEn,
                     investEn] {
        const QString id = agentPick->currentData().toString();
        for (const auto &av : agents_) {
            const QJsonObject a = av.toObject();
            if (jstr(a, "agent_id") != id)
                continue;
            generalUrl->setText(jstr(a, "general_url"));
            investUrl->setText(jstr(a, "investment_url"));
            generalEn->setChecked(
                !a.contains(QStringLiteral("general_enabled")) ||
                a.value(QStringLiteral("general_enabled")).toBool());
            investEn->setChecked(
                !a.contains(QStringLiteral("investment_enabled")) ||
                a.value(QStringLiteral("investment_enabled")).toBool());
        }
    };
    populate();
    connect(agentPick, &QComboBox::activated, &dlg, populate);
    auto *saveBiz = new QPushButton(QStringLiteral("儲存業務設定"), &dlg);
    connect(saveBiz, &QPushButton::clicked, &dlg,
            [this, agentPick, generalUrl, investUrl, generalEn, investEn] {
                QString inv = investUrl->text().trimmed();
                if (inv.isEmpty())
                    inv = generalUrl->text().trimmed();
                socket_->sendCommand(
                    QStringLiteral("ai_nexus_update_agent_business_settings"),
                    {{QStringLiteral("agent_id"),
                      agentPick->currentData().toString()},
                     {QStringLiteral("general_url"),
                      generalUrl->text().trimmed()},
                     {QStringLiteral("investment_url"), inv},
                     {QStringLiteral("general_enabled"),
                      generalEn->isChecked()},
                     {QStringLiteral("investment_enabled"),
                      investEn->isChecked()},
                     {QStringLiteral("request_id"), newId()}});
            });
    v->addWidget(saveBiz);

    // --- memory ---
    v->addWidget(makeLabel(QStringLiteral("協作記憶"), "section", &dlg));
    auto *memList = new QListWidget(&dlg);
    for (const auto &mv : memoryItems_) {
        const QJsonObject m = mv.toObject();
        memList->addItem(QStringLiteral("[%1] %2")
                             .arg(jstr(m, "kind"), jstr(m, "title")));
    }
    memList->setMaximumHeight(120);
    v->addWidget(memList);
    auto *memForm = new QFormLayout;
    auto *memTitle = new QLineEdit(&dlg);
    auto *memContent = new QLineEdit(&dlg);
    memForm->addRow(QStringLiteral("標題"), memTitle);
    memForm->addRow(QStringLiteral("內容"), memContent);
    v->addLayout(memForm);
    auto *addMem = new QPushButton(QStringLiteral("新增記憶"), &dlg);
    connect(addMem, &QPushButton::clicked, &dlg, [this, memTitle, memContent] {
        socket_->sendCommand(
            QStringLiteral("ai_nexus_add_memory"),
            {{QStringLiteral("kind"), QStringLiteral("note")},
             {QStringLiteral("title"), memTitle->text().trimmed()},
             {QStringLiteral("content"), memContent->text().trimmed()},
             {QStringLiteral("request_id"), newId()}});
    });
    v->addWidget(addMem);

    dlg.resize(560, 640);
    dlg.exec();
}

// ---------------- state ----------------

void CollabWindow::setBusy(bool busy) {
    busy_ = busy;
    sendBtn_->setEnabled(!busy);
    sendBtn_->setText(busy ? QStringLiteral("協作中…")
                           : QStringLiteral("送出協作"));
    cancelBtn_->setVisible(busy && !currentTaskId_.isEmpty());
}

void CollabWindow::setError(const QString &message) {
    errorLabel_->setText(message);
    errorLabel_->setVisible(!message.isEmpty());
}

void CollabWindow::flash(const QString &message) {
    statusLine_->setText(message);
    if (!message.isEmpty())
        setError(QString());
}
