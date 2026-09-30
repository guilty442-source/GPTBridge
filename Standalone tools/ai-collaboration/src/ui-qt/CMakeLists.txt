cmake_minimum_required(VERSION 3.20)
project(ai_collab_ui VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTORCC ON)
set(CMAKE_AUTOUIC ON)

find_package(Qt6 REQUIRED COMPONENTS Widgets WebEngineWidgets WebSockets Network)

qt_standard_project_setup()

qt_add_executable(ai-collab-ui WIN32
    main.cpp
    BackendSocket.h BackendSocket.cpp
    BrowserOpsHandler.h BrowserOpsHandler.cpp
    CollabWindow.h CollabWindow.cpp
)

target_link_libraries(ai-collab-ui PRIVATE
    Qt6::Widgets
    Qt6::WebEngineWidgets
    Qt6::WebSockets
    Qt6::Network
)
