QT += widgets websockets network
CONFIG += c++17
TARGET = star-chat-ui
TEMPLATE = app
DESTDIR = $$PWD/../../../dist

SOURCES += \
    main.cpp \
    BackendSocket.cpp \
    StarChatWindow.cpp

HEADERS += \
    BackendSocket.h \
    StarChatWindow.h
