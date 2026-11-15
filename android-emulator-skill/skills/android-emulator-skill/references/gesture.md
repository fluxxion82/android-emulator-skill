# gesture.py

Perform swipes, scrolls, long press

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- `--scroll {up,down}` names the direction the **content** moves; `--swipe` names what the
  **finger** does. They are opposites. (Conflating them was a real defect: `--scroll down`
  dragged lists back toward the top and reported success.)
- Directional swipes
- Custom swipe coordinates: `--swipe-path x1,y1,x2,y2`; drag and drop: `--drag x1,y1,x2,y2`
- Scroll (`--count N` repeats it) and long press
- `--refresh` pulls to refresh (a swipe down from the top; `--duration` defaults to 600 ms for it)
