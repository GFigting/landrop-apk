[app]
title = LAN Drop
package.name = landrop
package.domain = org.landrop
source.dir = .
source.include_exts = py,js,otf,png
version = 1.0.0
requirements = python3,kivy==2.3.0,qrcode
orientation = portrait
fullscreen = False
android.permissions = INTERNET,ACCESS_NETWORK_STATE,ACCESS_WIFI_STATE,CHANGE_WIFI_MULTICAST_STATE
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True
android.allow_backup = True
log_level = 2

[buildozer]
log_level = 2
warn_on_root = 1
