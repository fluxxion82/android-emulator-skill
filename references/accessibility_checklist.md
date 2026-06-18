# Android Accessibility Checklist

A practical checklist for Android UI accessibility, mapped directly to the issue
types emitted by `scripts/accessibility_audit.py`. The auditor walks the live
uiautomator UI hierarchy of the current screen and flags issues by severity.

## Quick Audit Command

```bash
# Audit the current screen
python scripts/accessibility_audit.py

# Show every issue (not just the grouped top types)
python scripts/accessibility_audit.py --verbose

# Save JSON + Markdown reports to a directory
python scripts/accessibility_audit.py --output audit-reports/

# Machine-readable output
python scripts/accessibility_audit.py --json

# Target a specific device
python scripts/accessibility_audit.py --serial emulator-5554
```

Flags: `--output`, `--serial`, `--json`, `--verbose`.

**CI gate:** the script exits `1` when any **critical** issue is present, else `0`.

**Tunables (environment, `ANDROID_EMU_` prefix):**

| Variable | Default | Effect |
|----------|---------|--------|
| `ANDROID_EMU_A11Y_MAX_NESTING` | `5` | Hierarchy depth above which nodes are flagged as `deep_nesting`. |
| `ANDROID_EMU_A11Y_TOP_ISSUES` | `10` | Number of grouped issue types shown in concise console output. |

---

## Critical (Must Fix — fails the CI gate)

### `missing_content_description`
**What it flags:** a clickable, enabled `Button` / `ImageButton` / `ImageView`
that has neither a content description nor visible text.
**Why:** TalkBack and other screen readers announce nothing usable, so the
control is unreachable for non-sighted users.
**Audit fix string:** *Add android:contentDescription to the element.*

XML:

```xml
<ImageButton
    android:id="@+id/btn_share"
    android:src="@drawable/ic_share"
    android:contentDescription="@string/share_post" />
```

Compose (icon-only / clickable image):

```kotlin
IconButton(onClick = ::onShare) {
    Icon(
        imageVector = Icons.Filled.Share,
        contentDescription = stringResource(R.string.share_post),
    )
}
```

---

## Warning (Should Fix)

### `small_touch_target` (`< 48dp`)
**What it flags:** a clickable, enabled element whose measured width **or**
height is less than `48dp` (`MIN_TOUCH_TARGET_SIZE = 48`).
**Why:** Material accessibility guidance requires a minimum touch target of
48x48dp so the control is reliably tappable.
**Audit fix string:** *Increase touch target to at least 48x48dp.*

XML (pad the tappable area without enlarging the visual icon):

```xml
<ImageButton
    android:id="@+id/btn_close"
    android:src="@drawable/ic_close"
    android:contentDescription="@string/close"
    android:minWidth="48dp"
    android:minHeight="48dp"
    android:background="?attr/selectableItemBackgroundBorderless" />
```

Compose:

```kotlin
IconButton(
    onClick = ::onClose,
    modifier = Modifier.sizeIn(minWidth = 48.dp, minHeight = 48.dp),
) {
    Icon(Icons.Filled.Close, contentDescription = stringResource(R.string.close))
}
```

### `edittext_missing_hint`
**What it flags:** an `EditText` with no `hint`, no `text`, and no content
description.
**Why:** without a hint the field has no accessible name, so screen readers
cannot tell users what to type.
**Audit fix string:** *Add android:hint to describe the expected input.*

XML (prefer a `TextInputLayout` so the hint persists as a floating label):

```xml
<com.google.android.material.textfield.TextInputLayout
    android:layout_width="match_parent"
    android:layout_height="wrap_content"
    android:hint="@string/email_address">

    <com.google.android.material.textfield.TextInputEditText
        android:id="@+id/input_email"
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:inputType="textEmailAddress" />
</com.google.android.material.textfield.TextInputLayout>
```

Compose:

```kotlin
TextField(
    value = email,
    onValueChange = { email = it },
    label = { Text(stringResource(R.string.email_address)) },
)
```

---

## Info (Nice to Have)

### `image_missing_description`
**What it flags:** an `ImageView` without a content description.
**Why:** informative images need alt text; purely decorative images should be
explicitly marked so screen readers skip them.
**Audit fix string:** *Add android:contentDescription, or set
importantForAccessibility='no' if decorative.*

XML — informative image:

```xml
<ImageView
    android:id="@+id/avatar"
    android:src="@drawable/profile"
    android:contentDescription="@string/user_avatar" />
```

XML — decorative image (silenced for screen readers):

```xml
<ImageView
    android:id="@+id/divider_icon"
    android:src="@drawable/ic_dot"
    android:importantForAccessibility="no" />
```

Compose — `contentDescription = null` marks the image decorative:

```kotlin
Image(
    painter = painterResource(R.drawable.profile),
    contentDescription = stringResource(R.string.user_avatar), // null if decorative
)
```

### `missing_resource_id`
**What it flags:** a clickable, enabled element with no `resource-id`.
**Why:** an `android:id` lets the element be reliably found by accessibility
tooling and UI tests (e.g. `navigator.py --find-id`).
**Audit fix string:** *Add android:id so the element can be reliably referenced
and tested.*

XML:

```xml
<Button
    android:id="@+id/btn_submit"
    android:text="@string/submit" />
```

Compose (`testTag` surfaces a stable identifier):

```kotlin
Button(
    onClick = ::onSubmit,
    modifier = Modifier.testTag("btn_submit"),
) {
    Text(stringResource(R.string.submit))
}
```

### `deep_nesting`
**What it flags:** a node deeper than `ANDROID_EMU_A11Y_MAX_NESTING` (default
`5`) levels in the view hierarchy.
**Why:** deeply nested layouts slow rendering and make screen-reader navigation
tedious.
**Audit fix string:** *Flatten the layout to reduce view-hierarchy depth.*

XML — flatten nested `LinearLayout`s with a single `ConstraintLayout`:

```xml
<androidx.constraintlayout.widget.ConstraintLayout
    android:layout_width="match_parent"
    android:layout_height="wrap_content">

    <TextView
        android:id="@+id/title"
        app:layout_constraintTop_toTopOf="parent"
        app:layout_constraintStart_toStartOf="parent" />

    <TextView
        android:id="@+id/subtitle"
        app:layout_constraintTop_toBottomOf="@id/title"
        app:layout_constraintStart_toStartOf="parent" />
</androidx.constraintlayout.widget.ConstraintLayout>
```

Compose composes flat by default; prefer a single `Column`/`Row`/`Box` over
wrapping layers, and use `Modifier` weights and arrangements instead of nested
containers.

### `long_text_block`
**What it flags:** a text element longer than 100 characters (also emitted at
`info`).
**Why:** long unbroken blocks are harder to read; ensure adequate line spacing
and consider splitting the content.
**Audit fix string:** *Ensure adequate line spacing and consider breaking up
the content.*

XML:

```xml
<TextView
    android:layout_width="match_parent"
    android:layout_height="wrap_content"
    android:lineSpacingMultiplier="1.3"
    android:text="@string/article_body" />
```

---

## Severity Summary

| Issue type | Severity | Trigger |
|------------|----------|---------|
| `missing_content_description` | critical | Clickable Button/ImageButton/ImageView with no content-desc and no text |
| `small_touch_target` | warning | Clickable element width or height `< 48dp` |
| `edittext_missing_hint` | warning | EditText with no hint, text, or content-desc |
| `image_missing_description` | info | ImageView with no content-desc |
| `missing_resource_id` | info | Clickable element with no resource-id |
| `deep_nesting` | info | Depth `>` `ANDROID_EMU_A11Y_MAX_NESTING` (default 5) |
| `long_text_block` | info | Text element longer than 100 characters |

Only **critical** issues fail the CI gate (`exit 1`). Warnings and info are
reported but do not block.
