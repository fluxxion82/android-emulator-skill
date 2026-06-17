"""Shared test fixtures and sample data for android-emulator-skill tests."""

from __future__ import annotations

# A representative `uiautomator dump` XML hierarchy used by unit tests so they
# can exercise parsing/audit logic without a connected device.
SAMPLE_UI_HIERARCHY = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.example.app" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[0,0][1080,2400]">
    <node index="0" text="Welcome" resource-id="com.example.app:id/title" class="android.widget.TextView" package="com.example.app" content-desc="" clickable="false" enabled="true" bounds="[40,200][1040,300]" />
    <node index="1" text="" resource-id="com.example.app:id/email" class="android.widget.EditText" package="com.example.app" content-desc="" clickable="true" enabled="true" focusable="true" bounds="[40,360][1040,460]" />
    <node index="2" text="Log In" resource-id="com.example.app:id/login_button" class="android.widget.Button" package="com.example.app" content-desc="Log in to your account" clickable="true" enabled="true" bounds="[40,520][1040,640]" />
    <node index="3" text="" resource-id="com.example.app:id/avatar" class="android.widget.ImageView" package="com.example.app" content-desc="" clickable="true" enabled="true" bounds="[900,40][1040,180]" />
  </node>
</hierarchy>"""
