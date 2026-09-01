package com.example.composefixture

import android.app.Activity
import android.os.Bundle
import android.os.StrictMode
import java.io.File

/**
 * Commits a StrictMode violation on purpose, so the real logcat line can be
 * recorded instead of guessed.
 *
 * `common/anr_pipeline._classify_anr` has a branch for StrictMode, and the only
 * input it had ever seen was the hand-typed
 * `D StrictMode: Slow operation detected on main thread` -- a line that
 * conflates StrictMode with ActivityManager's own `Slow operation:` bookkeeping
 * and that StrictMode does not print in any form. Recorded as
 * `logcat_strictmode_violation`.
 *
 * Deliberately NOT part of DefaultActivity: that one is what the
 * `uiautomator_compose_*` fixtures dump, and its behaviour must not drift.
 * This activity renders nothing and finishes immediately.
 *
 *     adb -s emulator-5554 shell am start -n \
 *       com.example.composefixture/.StrictModeActivity
 */
class StrictModeActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        StrictMode.setThreadPolicy(
            StrictMode.ThreadPolicy.Builder()
                .detectDiskReads()
                .detectDiskWrites()
                .penaltyLog()
                .build(),
        )
        // A disk read and a disk write on the main thread: two violations, so
        // the recording shows how StrictMode frames more than one.
        File(filesDir, "strictmode_probe.txt").writeText("probe")
        File(filesDir, "strictmode_probe.txt").readText()
        finish()
    }
}
