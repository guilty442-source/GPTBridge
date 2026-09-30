cmake_minimum_required(VERSION 3.20)
project(star_chat_ui VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTORCC ON)
set(CMAKE_AUTOUIC ON)

find_package(Qt6 REQUIRED COMPONENTS Widgets WebSockets Network)

qt_standard_project_setup()

qt_add_executable(star-chat-ui WIN32
    main.cpp
    BackendSocket.h BackendSocket.cpp
    StarChatWindow.h StarChatWindow.cpp
)

target_link_libraries(star-chat-ui PRIVATE
    Qt6::Widgets
    Qt6::WebSockets
    Qt6::Network
)
