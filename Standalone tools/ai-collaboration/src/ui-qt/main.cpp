// ai-collab-ui — native Qt surface for the ai-collaboration tool
// (C116/E180 native UI lane; replaces the Tauri/WebView2 renderer).
// Launch contract: --tool-window --tool-id=ai-collaboration, with the
// governed env supplying GPTBRIDGE_SOURCE_UI_WEBSOCKET_URL plus
// window geometry and title metadata. Fails closed when the session
// URL is absent or not loopback-ws.
#include <QApplication>
#include <QCommandLineParser>
#include <QMessageBox>
#include <QUrl>
#include <QUrlQuery>

#include "CollabWindow.h"

namespace {

QString envOr(const char *name, const QString &fallback = {}) {
    const QString v = qEnvironmentVariable(name);
    return v.isEmpty() ? fallback : v;
}

int envIntSafe(const char *name, int fallback) {
    const QByteArray raw = qgetenv(name);
    if (raw.isEmpty())
        return fallback;
    bool ok = false;
    const int v = raw.toInt(&ok);
    return ok ? v : fallback;
}

bool validSessionUrl(const QUrl &url) {
    if (url.scheme() != QLatin1String("ws"))
        return false;
    if (url.host() != QLatin1String("127.0.0.1"))
        return false;
    const int port = url.port();
    if (port < 1024 || port > 65535)
        return false;
    const QUrlQuery q(url);
    const QString token = q.queryItemValue(QStringLiteral("token"));
    static const QRegularExpression hex64(
        QStringLiteral("^[a-f0-9]{64}$"));
    if (!hex64.match(token.toLower()).hasMatch())
        return false;
    if (q.queryItemValue(QStringLiteral("instance")).isEmpty())
        return false;
    return true;
}

} // namespace

int main(int argc, char *argv[]) {
    QApplication app(argc, argv);
    QApplication::setApplicationName(QStringLiteral("ai-collab-ui"));
    QApplication::setOrganizationName(QStringLiteral("GPTBridge"));

    QCommandLineParser parser;
    QCommandLineOption toolWindow(QStringLiteral("tool-window"));
    QCommandLineOption toolIdOpt(QStringLiteral("tool-id"),
                                 QStringLiteral("tool id"),
                                 QStringLiteral("id"));
    parser.addOption(toolWindow);
    parser.addOption(toolIdOpt);
    parser.process(app);

    const QString toolId =
        parser.value(toolIdOpt).isEmpty()
            ? QStringLiteral("ai-collaboration")
            : parser.value(toolIdOpt);

    const QString wsUrl =
        envOr("GPTBRIDGE_SOURCE_UI_WEBSOCKET_URL");
    const QUrl url(wsUrl);
    if (!validSessionUrl(url)) {
        QMessageBox::critical(
            nullptr, QStringLiteral("AI 協作"),
            QStringLiteral(
                "受管工作階段能力不可用（SOURCE_UI_WEBSOCKET_URL），\n"
                "視窗以失敗關閉。"));
        return 2;
    }

    const QString title =
        envOr("GPTBRIDGE_SOURCE_UI_TITLE", QStringLiteral("AI 協作"));
    const int width = envIntSafe("GPTBRIDGE_SOURCE_UI_WIDTH", 1440);
    const int height = envIntSafe("GPTBRIDGE_SOURCE_UI_HEIGHT", 920);
    const int minW = envIntSafe("GPTBRIDGE_SOURCE_UI_MIN_WIDTH", 1120);
    const int minH = envIntSafe("GPTBRIDGE_SOURCE_UI_MIN_HEIGHT", 760);

    CollabWindow window(toolId, wsUrl);
    window.setWindowTitle(title);
    window.resize(width, height);
    window.setMinimumSize(minW, minH);
    window.show();
    return app.exec();
}
