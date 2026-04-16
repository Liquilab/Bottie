#!/usr/bin/env python3
with open("/opt/bottie/dashboard.py", "r") as f:
    lines = f.readlines()

new_lines = []
skip_bad_route = False
already_added = False

for i, line in enumerate(lines):
    # Skip any previously broken /health route attempts
    if 'elif page == "/health":' in line and not already_added:
        # Check if this is our good one or a broken one
        # Look ahead for render_health_page
        lookahead = "".join(lines[i:i+5])
        if "render_health_page" in lookahead and "self._send_html" in lookahead:
            # This is a good one, keep it
            new_lines.append(line)
            continue
        else:
            # Bad one, skip it and next few lines
            skip_bad_route = True
            continue

    if skip_bad_route:
        if line.strip().startswith("elif ") or line.strip().startswith("else:"):
            skip_bad_route = False
            # Don't skip this line, it's the next route
        else:
            continue

    # Insert /health route before the catch-all
    if not already_added and 'elif page in ("/", "/index.html"' in line:
        new_lines.append('        elif page == "/health":\n')
        new_lines.append('            try:\n')
        new_lines.append('                html = render_health_page(token=token, account=account)\n')
        new_lines.append('                self._send_html(html)\n')
        new_lines.append('            except Exception as e:\n')
        new_lines.append('                import traceback\n')
        new_lines.append('                self._send_html("<pre>Error: %s\\n%s</pre>" % (e, traceback.format_exc()), 500)\n')
        already_added = True

    new_lines.append(line)

with open("/opt/bottie/dashboard.py", "w") as f:
    f.writelines(new_lines)
print("Route fixed")
