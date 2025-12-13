# Quick Test Instructions

## 🚀 Fastest Way to Test (2 minutes)

### Step 1: Start an Emulator

```bash
# List available emulators
emulator -list-avds

# Start one (replace with your AVD name)
emulator -avd Pixel_5_API_33 &

# Wait ~30 seconds for it to boot
# You'll see the Android home screen
```

**Don't have an AVD?** Create one:
```bash
# List available system images
sdkmanager --list | grep system-images

# Download one (example)
sdkmanager "system-images;android-33;google_apis;x86_64"

# Create AVD
avdmanager create avd -n TestDevice -k "system-images;android-33;google_apis;x86_64"
```

### Step 2: Run Quick Test

```bash
cd android-skill
./quick_test.sh
```

**Expected output:**
```
🤖 Android Emulator Skill - Quick Test

✓ Found device: emulator-5554

Testing 5 core functions...

1️⃣  Launching Settings app...
   ✓ App launched
2️⃣  Mapping screen...
   ✓ Screen mapped
   Output: Screen: Settings (45 elements, 12 interactive)
3️⃣  Finding element...
   ✓ Element found
4️⃣  Testing gesture...
   ✓ Gesture worked
5️⃣  Testing keyboard...
   ✓ Button press worked

🎉 Success! All core functions work!
```

### Step 3: Try Manual Commands

```bash
cd skill/scripts

# Launch Settings
python app_launcher.py --launch com.android.settings

# See what's on screen
python screen_mapper.py

# Find and tap something
python navigator.py --find-text "Apps" --tap

# Go back
python keyboard.py --button back

# Swipe
python gesture.py --swipe up
```

## 📋 Full Test Suite (5 minutes)

Run comprehensive tests:

```bash
cd android-skill
./test_basic.sh
```

This tests:
- ✓ Device detection
- ✓ App launching
- ✓ Screen mapping
- ✓ Element finding
- ✓ Interactions (tap)
- ✓ Gestures (swipe)
- ✓ Keyboard input
- ✓ App termination

## 🎯 Real-World Test (Login Flow Example)

```bash
# 1. Launch your app (or use Settings as example)
python skill/scripts/app_launcher.py --launch com.android.settings

# 2. Map the screen to see what's available
python skill/scripts/screen_mapper.py --verbose

# 3. Find and tap "Apps"
python skill/scripts/navigator.py --find-text "Apps" --tap

# 4. Wait and check new screen
sleep 1
python skill/scripts/screen_mapper.py

# 5. Go back
python skill/scripts/keyboard.py --button back

# 6. Scroll down
python skill/scripts/gesture.py --scroll up --count 3

# 7. Take screenshot for comparison
adb shell screencap -p > screenshot.png
```

## 🐛 Troubleshooting

### "No devices connected"
```bash
# Check ADB
adb devices

# If empty, restart ADB
adb kill-server
adb start-server

# Start emulator
emulator -avd Pixel_5_API_33 &
```

### "Element not found"
```bash
# See what's actually there
python skill/scripts/screen_mapper.py --verbose

# List all interactive elements
python skill/scripts/navigator.py --list

# Use fuzzy matching (default)
python skill/scripts/navigator.py --find-text "app" --tap  # Finds "Apps"
```

### "UI dump failed"
```bash
# Try manually
adb shell uiautomator dump
adb shell cat /sdcard/window_dump.xml

# If empty, the screen might be locked or app crashed
```

### Python import errors
```bash
# Make sure you're in the right directory
cd android-skill/skill/scripts

# Or use absolute imports
export PYTHONPATH=/path/to/android-skill/skill/scripts
```

## ✅ Success Checklist

Your skill is working if you can:

- [x] Run `./quick_test.sh` without errors
- [x] See "Screen: MainActivity (X elements)" from screen_mapper.py
- [x] Find an element: `navigator.py --find-text "Apps"`
- [x] Tap works and you see the change on device
- [x] Swipe gesture visibly scrolls the screen
- [x] Back button works

## 🎓 Next Steps After Testing

1. **Test with your app**
   ```bash
   python skill/scripts/app_launcher.py --install /path/to/your-app.apk
   python skill/scripts/app_launcher.py --launch com.your.app
   python skill/scripts/screen_mapper.py
   ```

2. **Create test scenarios** (see TESTING.md)

3. **Use with Claude Code**
   - Move to `~/.claude/skills/android-emulator-skill`
   - Claude will auto-invoke scripts when you ask about Android testing

4. **Explore examples**
   ```bash
   # See TESTING.md for more examples
   cat TESTING.md
   ```

## 📊 What Makes This Different?

**Before (raw ADB):**
```bash
$ adb shell uiautomator dump && adb shell cat /sdcard/window_dump.xml
[847 lines of XML output...]
```

**After (our skill):**
```bash
$ python skill/scripts/screen_mapper.py
Screen: MainActivity (45 elements, 7 interactive)
Buttons: "Login", "Cancel", "Forgot Password"
EditTexts: 2 (0 filled)
```

**96% token reduction + semantic navigation!** 🎉

---

**Quick Start:** Just run `./quick_test.sh` and see if it works!
