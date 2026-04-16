#!/usr/bin/env python3
"""Fix CSS injection in f-string - move score CSS to CSS constant."""

with open("/opt/bottie/dashboard.py", "r") as f:
    content = f.read()

bad_css = """\n.score-badge { display:inline-block; padding:2px 8px; border-radius:4px; font-size:0.85rem; font-weight:600; margin-left:8px; font-family:monospace; }
.score-badge.live { background:rgba(255,50,50,0.15); color:#ff4444; animation: pulse 2s infinite; }
.score-badge.pre { background:rgba(100,100,100,0.15); color:#888; }
.score-badge.final { background:rgba(100,100,100,0.15); color:#aaa; }
.score-detail { font-size:0.7rem; font-weight:400; opacity:0.7; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.7} }
"""

# Remove from </style> location (inside f-string where it breaks)
content = content.replace(bad_css + "\n</style>", "</style>")
print("1. Removed CSS from f-string")

# Find the CSS constant and append score CSS there
# CSS is defined as CSS = """...""" or CSS = r"""..."""
import re
# Find the CSS variable definition
m = re.search(r'^CSS\s*=\s*(?:r)?"""', content, re.MULTILINE)
if m:
    # Find the closing """ of the CSS constant
    start = m.end()
    # Find closing triple quote (not inside the CSS content)
    idx = start
    while True:
        end = content.find('"""', idx)
        if end < 0:
            print("ERROR: could not find closing CSS triple quote")
            break
        # Make sure it's the actual closing, not another f-string
        # Check it's followed by newline or end
        if end > start + 100:  # CSS should be long
            content = content[:end] + bad_css + content[end:]
            print("2. Added CSS to CSS constant")
            break
        idx = end + 3
else:
    print("WARN: no CSS constant found")
    # Alternate: the CSS might be inline. Just escape braces.
    escaped = bad_css.replace("{", "{{").replace("}", "}}")
    # Find where it is now (already removed from </style>, so it might be gone)
    # Let's just add it as a separate <style> tag in the page_wrap header
    content = content.replace(
        '<title>Bottie',
        '<style>\n.score-badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:0.85rem;font-weight:600;margin-left:8px;font-family:monospace}\n.score-badge.live{background:rgba(255,50,50,0.15);color:#ff4444;animation:pulse 2s infinite}\n.score-badge.pre{background:rgba(100,100,100,0.15);color:#888}\n.score-badge.final{background:rgba(100,100,100,0.15);color:#aaa}\n.score-detail{font-size:0.7rem;font-weight:400;opacity:0.7}\n@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.7}}\n</style>\n  <title>Bottie'
    )
    print("2. Added CSS as separate style tag (escaped)")

with open("/opt/bottie/dashboard.py", "w") as f:
    f.write(content)

print("Done!")
