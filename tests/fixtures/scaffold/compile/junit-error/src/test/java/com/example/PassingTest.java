package com.example;

import static org.junit.Assert.assertTrue;

import org.junit.Test;

/**
 * The passing half of the contrast.
 *
 * <p>Present so the recorded XML set contains a green class alongside the
 * errored one: a parser that reports "all tests failed" and a parser that
 * reports nothing at all look identical against a corpus where everything
 * failed.
 */
public class PassingTest {

    @Test
    public void passes() {
        assertTrue(true);
    }
}
