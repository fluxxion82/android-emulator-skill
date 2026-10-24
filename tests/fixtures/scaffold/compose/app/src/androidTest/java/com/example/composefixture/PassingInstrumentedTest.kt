package com.example.composefixture

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith

/** Green instrumented suite: one test, no UI, deterministic. */
@RunWith(AndroidJUnit4::class)
class PassingInstrumentedTest {
    @Test
    fun targetContextIsTheFixtureApp() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        assertEquals("com.example.composefixture", context.packageName)
    }
}
