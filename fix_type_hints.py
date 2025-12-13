#!/usr/bin/env python3
"""
Fix Python 3.10+ type hints to be compatible with Python 3.8+
Replaces X | Y with Union[X, Y] or Optional[X]
"""

import re
from pathlib import Path

def fix_file(filepath):
    """Fix type hints in a single file"""
    with open(filepath, 'r') as f:
        content = f.read()

    original = content

    # Check if typing imports exist
    has_typing = 'from typing import' in content or 'import typing' in content

    # Find all X | None patterns and replace with Optional[X]
    # Match type hints like: str | None, int | None, etc.
    content = re.sub(r':\s*(\w+)\s*\|\s*None', r': Optional[\1]', content)
    content = re.sub(r'->\s*(\w+)\s*\|\s*None', r'-> Optional[\1]', content)

    # Handle more complex patterns like dict[str, Any] | None
    content = re.sub(r':\s*([\w\[\],\s]+)\s*\|\s*None', r': Optional[\1]', content)
    content = re.sub(r'->\s*([\w\[\],\s]+)\s*\|\s*None', r'-> Optional[\1]', content)

    # Fix dict[str, Any] to just dict (Python 3.8 doesn't support subscripting dict in type hints)
    content = re.sub(r'dict\[str,\s*Any\]', 'dict', content)
    content = re.sub(r'dict\[str,\s*str\]', 'dict', content)
    content = re.sub(r'list\[dict\[str,\s*Any\]\]', 'list', content)
    content = re.sub(r'list\[dict\]', 'list', content)

    content = re.sub(r'list\[Element\]', 'list', content)
    content = re.sub(r'list\[str\]', 'list', content)
    content = re.sub(r'tuple\[int,\s*int,\s*int,\s*int\]', 'tuple', content)
    content = re.sub(r'tuple\[int,\s*int\]', 'tuple', content)
    content = re.sub(r'tuple\[bool,\s*str\]', 'tuple', content)
    content = re.sub(r'tuple\[bool,\s*list\]', 'tuple', content)
    content = re.sub(r'tuple\[bool,\s*dict\]', 'tuple', content)
    content = re.sub(r'tuple\[str,\s*int,\s*int\]', 'tuple', content)
    content = re.sub(r'tuple\[int,\s*int,\s*int\]', 'tuple', content)

    # Add Optional import if we made changes and it's not there
    if content != original and 'Optional' in content and 'from typing import' in content:
        # Add Optional to existing import if not there
        if 'Optional' not in content.split('from typing import')[1].split('\n')[0]:
            content = re.sub(
                r'from typing import ([^\n]+)',
                lambda m: f"from typing import {m.group(1)}, Optional" if 'Optional' not in m.group(1) else m.group(0),
                content,
                count=1
            )
    elif content != original and 'Optional' in content and 'from typing import' not in content:
        # Add new typing import
        content = re.sub(
            r'(import \w+\n)',
            r'\1from typing import Optional\n',
            content,
            count=1
        )

    # Only write if changed
    if content != original:
        with open(filepath, 'w') as f:
            f.write(content)
        return True
    return False

# Fix all Python files in scripts directory
scripts_dir = Path(__file__).parent / 'skill' / 'scripts'
fixed_count = 0

for py_file in scripts_dir.rglob('*.py'):
    if fix_file(py_file):
        print(f"Fixed: {py_file.relative_to(scripts_dir.parent)}")
        fixed_count += 1

print(f"\nFixed {fixed_count} files")
