package com.example.composefixture

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextField
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay

/**
 * Six screens of one small shopping flow, so a "multi-screen walk" in the
 * recorded corpus is a walk and not six captures of one screen.
 *
 * Reachable two ways, on purpose:
 *
 * - by tapping, as an agent would: every screen has a button to the next one;
 * - directly, for a deterministic recording: `--ei screen N` (1..6) picks the
 *   screen the activity opens on.
 *
 *     adb shell am start -W -n com.example.composefixture/.WalkActivity --ei screen 3
 *
 * Each screen fits without scrolling, because uiautomator dumps only what is
 * on screen. The root sets `testTagsAsResourceId`, the remediation the skill
 * documents, so the controls carry resource-ids.
 *
 * Nothing here animates and nothing changes without input: every screen
 * reaches idle, so a dump of it succeeds. The two activities below exist for
 * the opposite cases.
 */
class WalkActivity : ComponentActivity() {
    @OptIn(ExperimentalComposeUiApi::class)
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val first = intent.getIntExtra("screen", 1).coerceIn(1, 6)
        setContent {
            MaterialTheme {
                var screen by remember { mutableIntStateOf(first) }
                Column(
                    modifier = Modifier
                        .semantics { testTagsAsResourceId = true }
                        .fillMaxSize()
                        .safeDrawingPadding()
                        .padding(horizontal = 12.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    when (screen) {
                        1 -> CatalogScreen(go = { screen = it })
                        2 -> ProductScreen(go = { screen = it })
                        3 -> CartScreen(go = { screen = it })
                        4 -> CheckoutScreen(go = { screen = it })
                        5 -> ConfirmationScreen(go = { screen = it })
                        else -> SettingsScreen(go = { screen = it })
                    }
                }
            }
        }
    }
}

@Composable
private fun Title(text: String) {
    Text(text = text, modifier = Modifier.testTag("screen_title"))
}

@Composable
private fun CatalogScreen(go: (Int) -> Unit) {
    Title("Catalog")
    val products = listOf(
        "Trail Mug" to "$18.00",
        "Canvas Tote" to "$23.00",
        "Field Notebook" to "$9.50",
        "Enamel Pin" to "$6.00",
    )
    products.forEachIndexed { index, (name, price) ->
        Card(
            onClick = { go(2) },
            modifier = Modifier
                .fillMaxWidth()
                .testTag("product_card_$index"),
        ) {
            Column(modifier = Modifier.padding(8.dp)) {
                Text(name)
                Text(price)
            }
        }
    }
    Button(onClick = { go(3) }, modifier = Modifier.testTag("open_cart_button")) {
        Text("Open cart")
    }
    OutlinedButton(onClick = { go(6) }, modifier = Modifier.testTag("open_settings_button")) {
        Text("Settings")
    }
}

@Composable
private fun ProductScreen(go: (Int) -> Unit) {
    Title("Trail Mug")
    Text("$18.00")
    Text("Double-walled steel, 350 ml. Keeps a drink hot for six hours.")
    Text("In stock")
    Button(onClick = { go(3) }, modifier = Modifier.testTag("add_to_cart_button")) {
        Text("Add to cart")
    }
    OutlinedButton(onClick = { go(1) }, modifier = Modifier.testTag("back_to_catalog_button")) {
        Text("Back to catalog")
    }
}

@Composable
private fun CartScreen(go: (Int) -> Unit) {
    var mugs by remember { mutableIntStateOf(1) }
    Title("Cart")
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text("Trail Mug")
        OutlinedButton(
            onClick = { if (mugs > 1) mugs -= 1 },
            modifier = Modifier.testTag("mug_fewer_button"),
        ) { Text("Fewer") }
        Text(text = "Quantity $mugs", modifier = Modifier.testTag("mug_quantity"))
        OutlinedButton(
            onClick = { mugs += 1 },
            modifier = Modifier.testTag("mug_more_button"),
        ) { Text("More") }
    }
    Text("Canvas Tote, quantity 1")
    Text(text = "Total $${18 * mugs + 23}.00", modifier = Modifier.testTag("cart_total"))
    Button(onClick = { go(4) }, modifier = Modifier.testTag("checkout_button")) {
        Text("Checkout")
    }
    OutlinedButton(onClick = { go(1) }, modifier = Modifier.testTag("keep_shopping_button")) {
        Text("Keep shopping")
    }
}

@Composable
private fun CheckoutScreen(go: (Int) -> Unit) {
    var name by remember { mutableStateOf("") }
    var street by remember { mutableStateOf("") }
    var city by remember { mutableStateOf("") }
    var giftWrap by remember { mutableStateOf(false) }
    Title("Checkout")
    TextField(
        value = name,
        onValueChange = { name = it },
        label = { Text("Full name") },
        modifier = Modifier.fillMaxWidth().testTag("name_field"),
    )
    TextField(
        value = street,
        onValueChange = { street = it },
        label = { Text("Street address") },
        modifier = Modifier.fillMaxWidth().testTag("street_field"),
    )
    TextField(
        value = city,
        onValueChange = { city = it },
        label = { Text("City") },
        modifier = Modifier.fillMaxWidth().testTag("city_field"),
    )
    Row(verticalAlignment = Alignment.CenterVertically) {
        Checkbox(
            checked = giftWrap,
            onCheckedChange = { giftWrap = it },
            modifier = Modifier.testTag("gift_wrap_checkbox"),
        )
        Text("Gift wrap")
    }
    Button(onClick = { go(5) }, modifier = Modifier.testTag("place_order_button")) {
        Text("Place order")
    }
}

@Composable
private fun ConfirmationScreen(go: (Int) -> Unit) {
    Title("Order placed")
    Text("Order #5107")
    Text("Arrives Thursday")
    Text("A receipt was sent to the address on file.")
    Button(onClick = { go(6) }, modifier = Modifier.testTag("open_settings_button")) {
        Text("Open settings")
    }
    OutlinedButton(onClick = { go(1) }, modifier = Modifier.testTag("back_to_catalog_button")) {
        Text("Back to catalog")
    }
}

@Composable
private fun SettingsScreen(go: (Int) -> Unit) {
    var orderUpdates by remember { mutableStateOf(true) }
    var darkTheme by remember { mutableStateOf(false) }
    var cellular by remember { mutableStateOf(false) }
    Title("Settings")
    listOf(
        Triple("Order updates", orderUpdates) { value: Boolean -> orderUpdates = value },
        Triple("Dark theme", darkTheme) { value: Boolean -> darkTheme = value },
        Triple("Use cellular data", cellular) { value: Boolean -> cellular = value },
    ).forEachIndexed { index, (label, checked, onChange) ->
        Row(verticalAlignment = Alignment.CenterVertically) {
            Switch(
                checked = checked,
                onCheckedChange = onChange,
                modifier = Modifier.testTag("setting_switch_$index"),
            )
            Text(label)
        }
    }
    Text("Version 1.0")
    OutlinedButton(onClick = { go(1) }, modifier = Modifier.testTag("back_to_catalog_button")) {
        Text("Back to catalog")
    }
}

/**
 * Text that appears by itself, after a delay, with no input.
 *
 * Opens on "Placing order"; `delay_ms` later (default 5000) the same node reads
 * "Order confirmed". Nothing animates while it waits, so a dump taken before
 * the change succeeds and shows the first text, and one taken after shows the
 * second. That pair is ground truth for waiting on text: the transition the
 * Settings top/scrolled fixtures show was caused by a swipe.
 *
 *     adb shell am start -W -n com.example.composefixture/.DelayedActivity --ei delay_ms 12000
 */
class DelayedActivity : ComponentActivity() {
    @OptIn(ExperimentalComposeUiApi::class)
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val delayMs = intent.getIntExtra("delay_ms", 5000).coerceIn(0, 120_000)
        setContent {
            MaterialTheme {
                var confirmed by remember { mutableStateOf(false) }
                LaunchedEffect(Unit) {
                    delay(delayMs.toLong())
                    confirmed = true
                }
                Column(
                    modifier = Modifier
                        .semantics { testTagsAsResourceId = true }
                        .fillMaxSize()
                        .safeDrawingPadding()
                        .padding(horizontal = 12.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Title("Checkout")
                    Text(
                        text = if (confirmed) "Order confirmed" else "Placing order",
                        modifier = Modifier.testTag("order_status"),
                    )
                    if (confirmed) {
                        Text("Order #5107")
                    }
                }
            }
        }
    }
}

/**
 * A screen that never settles, four ways, to find out which of them stops
 * `uiautomator dump` from reading it. `--es mode` picks one:
 *
 * - `ticker` (default): a Compose Text whose content changes every 100 ms.
 * - `animation`: an endless alpha animation on a box. Pixels change every
 *   frame; the semantics tree does not.
 * - `progress`: an indeterminate CircularProgressIndicator, the spinner an
 *   ordinary loading screen shows.
 * - `view_ticker`: an android.widget.TextView whose text changes every 100 ms.
 *
 * What each one makes the dump answer is recorded, not assumed: see the
 * `uiautomator_never_idle_*` fixtures. As recorded on API 33 and 35, the three
 * Compose modes dump normally and only `view_ticker` yields "could not get
 * idle state".
 *
 *     adb shell am start -W -n com.example.composefixture/.NeverIdleActivity --es mode ticker
 */
class NeverIdleActivity : ComponentActivity() {
    @OptIn(ExperimentalComposeUiApi::class)
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val mode = intent.getStringExtra("mode") ?: "ticker"
        if (mode == "view_ticker") {
            val counter = TextView(this)
            val handler = Handler(Looper.getMainLooper())
            var ticks = 0
            val tick = object : Runnable {
                override fun run() {
                    ticks += 1
                    counter.text = "Synced $ticks items"
                    handler.postDelayed(this, 100)
                }
            }
            setContentView(counter)
            handler.post(tick)
            return
        }
        setContent {
            MaterialTheme {
                Column(
                    modifier = Modifier
                        .semantics { testTagsAsResourceId = true }
                        .fillMaxSize()
                        .safeDrawingPadding()
                        .padding(horizontal = 12.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Title("Syncing")
                    when (mode) {
                        "animation" -> EndlessAnimation()
                        "progress" -> CircularProgressIndicator(
                            modifier = Modifier.testTag("sync_spinner"),
                        )
                        else -> Ticker()
                    }
                    Text("Do not close the app")
                }
            }
        }
    }
}

@Composable
private fun Ticker() {
    var ticks by remember { mutableIntStateOf(0) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(100)
            ticks += 1
        }
    }
    Text(text = "Synced $ticks items", modifier = Modifier.testTag("sync_counter"))
}

@Composable
private fun EndlessAnimation() {
    val transition = rememberInfiniteTransition(label = "pulse")
    val alpha by transition.animateFloat(
        initialValue = 0.2f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 600, easing = LinearEasing),
            repeatMode = RepeatMode.Reverse,
        ),
        label = "pulse_alpha",
    )
    Box(
        modifier = Modifier
            .size(48.dp)
            .alpha(alpha)
            .background(Color(0xFF3F51B5))
            .testTag("sync_pulse"),
    )
}
