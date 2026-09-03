package com.example;

import static org.junit.Assert.assertTrue;

import org.junit.Ignore;
import org.junit.Test;

/**
 * The skipped case, and a probe for the {@code <error>} one.
 *
 * <p>{@code @Ignore} is the only way a Gradle JUnit report gets a
 * {@code <skipped/>} child, and `parse_junit_xml` counts those, so the corpus
 * needs a real one rather than a hand-written {@code <skipped/>}.
 *
 * <p>The second method throws something that is NOT an AssertionError, which is
 * the usual guess at what earns an {@code <error>} element rather than a
 * {@code <failure>}. See the recorded XML for what Gradle actually wrote.
 */
public class SkippedAndErrorTest {

    @Ignore("recorded on purpose: this is what a skipped test looks like")
    @Test
    public void ignoredOnPurpose() {
        assertTrue(false);
    }

    @Test
    public void throwsSomethingThatIsNotAnAssertionError() {
        throw new UnsupportedOperationException("not an assertion failure");
    }
}
