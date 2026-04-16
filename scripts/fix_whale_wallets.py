#!/usr/bin/env python3
import re

with open("/opt/bottie/scripts/whale_monitor.py", "r") as f:
    content = f.read()

old_pattern = r'WALLETS = \{[^}]+\}'
new_wallets = '''WALLETS = {
    "kch123": "0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee",
    "TennisEdge": "0xe30e74595517de48f1fb19f4553dd3d9f1e96b87",
    "FazeS1mple": "0x13414a77a4be48988851c73dfd824d0168e70853",
    "kahe_cs2": "0x88d17ad6bf91ca0935f6e70b37ebe3db92b618da",
    "texaskid_mlb": "0xc8075693f48668a264b9fa313b47f52712fcc12b",
    "CBB_Edge": "0x163eff4d251df4bfc95c49f4d90cd1bf224edc5b",
    "Samojako": "0xbca1b1e6d78efc7c04e13ec8277a4df1ecd8dd13",
}'''

content = re.sub(old_pattern, new_wallets, content)

with open("/opt/bottie/scripts/whale_monitor.py", "w") as f:
    f.write(content)
print("OK")
