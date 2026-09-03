package com.example;

import static org.junit.Assert.assertEquals;

import org.junit.Before;
import org.junit.Test;

/**
 * Two tests whose {@code @Before} throws, so neither method body ever runs.
 *
 * <p>MEASURED, and not what this file was first written to demonstrate: Gradle's
 * JUnit XML writer records a throwing {@code @Before} as
 * {@code <failure type="java.lang.IllegalStateException">} on the suite's
 * {@code failures} count, with {@code errors="0"}. The {@code <error>} element
 * a reader might expect for a setup exception does not appear. Both testcases
 * are still emitted by name even though neither body executed, and each carries
 * the same stack trace rooted at {@code setUp}.
 *
 * <p>That is the whole reason this scaffold exists rather than a hand-written
 * sample XML: the obvious guess about the element name is wrong, and a parser
 * written against the guess would report this class as green.
 */
public class BeforeThrowsTest {

    private String fixture;

    @Before
    public void setUp() {
        // Guarded by a runtime-parsed constant so javac cannot prove the
        // assignment below is unreachable and reject the file.
        if (Boolean.parseBoolean("true")) {
            throw new IllegalStateException("fixture setup failed on purpose");
        }
        fixture = "ready";
    }

    @Test
    public void erroredBySetup() {
        assertEquals("ready", fixture);
    }

    @Test
    public void alsoErroredBySetup() {
        assertEquals("ready", fixture);
    }
}
