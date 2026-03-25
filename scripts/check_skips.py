import json, urllib.request

API = "https://data-api.polymarket.com"
US = "0x9f23f6d5d18f9Fc5aeF42EFEc8f63a7db3dB6D15"

def g(u):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent":"B/1","Accept":"application/json"}),
        timeout=30
    ).read())

our = [p for p in (g(f"{API}/positions?user={US}&limit=500&sizeThreshold=0.01") or []) if float(p.get("size",0)) > 0.01]

# Check Pistons event
print("=== Pistons/Wizards posities ===")
for p in our:
    slug = (p.get("eventSlug") or "").strip()
    title = p.get("title") or "?"
    if "det-was" in slug or "pistons" in title.lower() or "wizards" in title.lower():
        out = p.get("outcome", "?")
        val = float(p.get("size",0)) * float(p.get("curPrice",0))
        print(f"  {title[:55]} | {out} | ${val:.2f} | slug={slug}")

has_ou = any("Pistons" in (p.get("title") or "") and "O/U" in (p.get("title") or "") for p in our)
print(f"\n  Hebben we Pistons O/U? {'JA' if has_ou else 'NEE'}")
if not has_ou:
    print("  >> sovereign O/U 233.5 werd ONTERECHT geskipt!")

# Check Sporting/Bodo event
print("\n=== Sporting/Bodo posities ===")
for p in our:
    slug = (p.get("eventSlug") or "").strip()
    title = p.get("title") or "?"
    if "spo1-bog1" in slug or "sporting" in title.lower() or "bod" in title.lower():
        out = p.get("outcome", "?")
        val = float(p.get("size",0)) * float(p.get("curPrice",0))
        print(f"  {title[:55]} | {out} | ${val:.2f} | slug={slug}")

has_btts = any("Sporting" in (p.get("title") or "") and "Both Teams" in (p.get("title") or "") for p in our)
print(f"\n  Hebben we Sporting BTTS? {'JA' if has_btts else 'NEE'}")
if not has_btts:
    print("  >> Cannae BTTS werd ONTERECHT geskipt!")
