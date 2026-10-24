package com.example.composefixture

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.fail
import org.junit.Test
import org.junit.runner.RunWith

/** Mixed instrumented suite: one test passes, one fails on purpose. */
@RunWith(AndroidJUnit4::class)
class FailingInstrumentedTest {
    @Test
    fun passesAlongsideTheFailure() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        assertEquals("com.example.composefixture", context.packageName)
    }

    @Test
    fun failsOnPurpose() {
        fail("recorded failure")
    }
}
