cmake_minimum_required(VERSION 3.16)
# Keep target configuration alongside its build artifacts.
if(NOT DEFINED SDKCONFIG)
    set(SDKCONFIG "${CMAKE_BINARY_DIR}/sdkconfig")
endif()
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
set(COMPONENTS main)
project(iphone_hand VERSION 0.2.0)
