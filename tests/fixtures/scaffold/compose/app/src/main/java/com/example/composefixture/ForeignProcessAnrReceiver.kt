package com.example.composefixture

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * AnrReceiver's block, run in a process NAMED after another package.
 *
 * The manifest declares this receiver with `android:process="com.example.victim"`:
 * a global process name an app may choose freely, which runs under THIS
 * app's uid. ActivityManager names an ANR by the process (`ANR in
 * <processName>`, ProcessErrorStateRecord prints `mApp.processName`), so this
 * app's own stall is reported as `ANR in com.example.victim` -- the case the
 * ANR reader must not file under that package. Recorded as
 * `logcat_anr_foreign_process_records`, with the stalled pid's uid as `ps`
 * answered while it was alive. Provoke it with
 *
 *     adb -s emulator-5554 shell am broadcast -f 0x10000000 \
 *       -n com.example.composefixture/.ForeignProcessAnrReceiver \
 *       -a com.example.composefixture.PROVOKE_FOREIGN_PROCESS_ANR
 */
class ForeignProcessAnrReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        // Exported, so anything can name this component: block only on the
        // provoking action, never on whatever else is sent to it.
        if (intent.action != PROVOKE_ACTION) return
        Thread.sleep(BLOCK_MS)
    }

    private companion object {
        const val PROVOKE_ACTION = "com.example.composefixture.PROVOKE_FOREIGN_PROCESS_ANR"

        /** Comfortably past the 10s foreground-broadcast dispatch timeout. */
        const val BLOCK_MS = 40_000L
    }
}
