package com.example.composefixture

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * A receiver that deliberately blocks the main thread, so a real ANR can be
 * recorded rather than imagined.
 *
 * `common/anr_pipeline.parse_logcat_anr` claims to recognise ActivityManager's
 * "ANR in <pkg> (<component>)" line, and every test of that claim was written
 * against a hand-typed guess at the line -- the repo's defining bug. Android
 * will not produce an ANR on request, so the only way to get ground truth is
 * to make an app that earns one.
 *
 * A FOREGROUND broadcast (`-f 0x10000000`, FLAG_RECEIVER_FOREGROUND) has a 10s
 * dispatch timeout, so sleeping well past it is enough. This needs no touch
 * input, which matters: the recorder must never be the thing that taps a
 * screen. Provoke it with
 *
 *     adb -s emulator-5554 shell am broadcast -f 0x10000000 \
 *       -n com.example.composefixture/.AnrReceiver \
 *       -a com.example.composefixture.PROVOKE_ANR
 *
 * The app recovers on its own once the sleep ends; nothing has to be killed.
 */
class AnrReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        Thread.sleep(BLOCK_MS)
    }

    private companion object {
        /** Comfortably past the 10s foreground-broadcast dispatch timeout. */
        const val BLOCK_MS = 40_000L
    }
}
