QT += widgets webenginewidgets websockets network
CONFIG += c++17
TARGET = ai-collab-ui
TEMPLATE = app
DESTDIR = $$PWD/../../dist

SOURCES += \
    main.cpp \
    BackendSocket.cpp \
    BrowserOpsHandler.cpp \
    CollabWindow.cpp

HEADERS += \
    BackendSocket.h \
    BrowserOpsHandler.h \
    CollabWindow.h
