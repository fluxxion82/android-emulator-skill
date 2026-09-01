package com.example.composefixture

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextField
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.painter.ColorPainter
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.unit.dp

/**
 * Variant 1 — what a new Android app looks like out of the box.
 *
 * No `testTagsAsResourceId`, so every node in the uiautomator dump has an EMPTY
 * `resource-id`. Recorded as `uiautomator_compose_default`.
 */
class DefaultActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                FixtureScreen(rootModifier = Modifier)
            }
        }
    }
}

/**
 * Variant 2 — the remediation the skill documents.
 *
 * `Modifier.semantics { testTagsAsResourceId = true }` on the root makes every
 * `Modifier.testTag` below it surface as `resource-id` in the accessibility
 * tree. Recorded as `uiautomator_compose_testtags`.
 *
 * The composable is shared with [DefaultActivity] and already carries its
 * `testTag`s, so the two dumps are the same tree and differ only in whether the
 * `resource-id` attribute is populated.
 */
class TestTagsActivity : ComponentActivity() {
    @OptIn(ExperimentalComposeUiApi::class)
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                FixtureScreen(
                    rootModifier = Modifier.semantics { testTagsAsResourceId = true },
                )
            }
        }
    }
}

/**
 * Every case the "which nodes are interactive?" question has to get right.
 *
 * Deliberately compact: the whole screen must fit without scrolling, because
 * uiautomator only dumps what is on screen.
 */
@Composable
private fun FixtureScreen(rootModifier: Modifier) {
    var email by remember { mutableStateOf("") }
    var checked by remember { mutableStateOf(false) }
    var switched by remember { mutableStateOf(true) }

    Column(
        modifier = rootModifier
            .fillMaxSize()
            .safeDrawingPadding()
            .padding(horizontal = 12.dp),
        verticalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        // Non-interactive label: text, no click handler, no role.
        Text(
            text = "Fixture Screen",
            modifier = Modifier.testTag("header_label"),
        )

        // Interactive, labelled: Role.Button + a Text child that merges up.
        Button(
            onClick = {},
            modifier = Modifier.testTag("submit_button"),
        ) {
            Text("Submit Order")
        }

        // Clickable container: Modifier.clickable sets mergeDescendants, so all
        // three Text children collapse into ONE node whose text is their
        // concatenation. Naive per-Text traversal double-counts these.
        Card(
            onClick = {},
            modifier = Modifier
                .fillMaxWidth()
                .testTag("order_card"),
        ) {
            Column(modifier = Modifier.padding(8.dp)) {
                Text("Order #4821")
                Text("2 items")
                Text("Ships tomorrow")
            }
        }

        // Editable text.
        TextField(
            value = email,
            onValueChange = { email = it },
            label = { Text("Email address") },
            modifier = Modifier
                .fillMaxWidth()
                .testTag("email_field"),
        )

        // Checkable, with the label as a SIBLING (not merged into it) — the
        // common Compose layout, and the reason a checkbox often has no text.
        Row(verticalAlignment = Alignment.CenterVertically) {
            Checkbox(
                checked = checked,
                onCheckedChange = { checked = it },
                modifier = Modifier.testTag("remember_checkbox"),
            )
            Text("Remember me")
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Switch(
                checked = switched,
                onCheckedChange = { switched = it },
                modifier = Modifier.testTag("dark_theme_switch"),
            )
            Text("Dark theme")
        }

        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            // Interactive and COMPLETELY unlabelled: clickable, but no text and
            // no contentDescription. Deciding what to do with this node is what
            // separates "floods the output" from "silently drops controls".
            IconButton(
                onClick = {},
                modifier = Modifier.testTag("icon_only_button"),
            ) {
                Box24(Color(0xFF3F51B5))
            }
            // Labelled image: contentDescription, but not clickable.
            Image(
                painter = ColorPainter(Color(0xFF4CAF50)),
                contentDescription = "Company logo",
                modifier = Modifier
                    .size(32.dp)
                    .testTag("company_logo"),
            )
        }

        // Scrollable list. LazyColumn reports scrollable=true and materialises
        // only the visible items.
        LazyColumn(
            modifier = Modifier
                .fillMaxWidth()
                .height(180.dp)
                .testTag("item_list"),
        ) {
            items(8) { index ->
                Text(
                    text = "List item $index",
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(vertical = 6.dp),
                )
            }
        }
    }
}

/** A 24dp coloured square. Avoids pulling in a drawable resource or icon pack. */
@Composable
private fun Box24(color: Color) {
    Column(modifier = Modifier.size(24.dp).background(color)) {}
}
