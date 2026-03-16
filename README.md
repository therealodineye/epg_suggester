# EPG Suggester — Dispatcharr Plugin

A Dispatcharr plugin that scans channels without EPG assignments and intelligently suggests the best matching EPG entry using fuzzy name matching and callsign detection.

[![Dispatcharr plugin](https://img.shields.io/badge/Dispatcharr-plugin-8A2BE2)](https://github.com/Dispatcharr/Dispatcharr)

---

## What It Does

When you have hundreds of IPTV channels with no EPG assigned, manually matching them one by one is painful. This plugin automates it.

It handles the messy reality of IPTV naming:

| Channel name in M3U | Matches EPG entry |
|---|---|
| `US\| CNN VIP HD` | `CNN` |
| `NO: SF-KANALEN ⱽᴵᴾ` | `NO: SF-KANALEN` |
| `PRIME: NBC ST. LOUIS NEWS (KSDK) ᴿᴬᵂ [FHD]` | `KSDK-DT` |
| `GO: ESPNEWS` | `ESPNEWS HD` |
| `SE: SVT BARN ᴴᴰ ⱽᴵᴾ` | `SE: SVT Barn ᴴᴰ` |

### Matching pipeline

1. **Strip noise** — Unicode superscript tags (`ᴴᴰ ⱽᴵᴾ ᴿᴬᵂ ᶠᴴᴰ`), quality tags (`HD`, `4K`, `UHD`), misc noise (`VIP`, `backup`, `+1`)
2. **Normalise prefixes** — country codes (`NO:`, `SE:`, `UK:`) are kept for country-aware matching; provider prefixes (`GO:`, `NOW:`, `VIP:`, `PRIME:`) are stripped
3. **Callsign matching** — channels containing a callsign like `(KSDK)` or `(WCAU)` are matched directly to EPG entries named `KSDK-DT`, `WCAU-DT` etc. at score 100
4. **Fuzzy scoring** — token overlap + SequenceMatcher ratio + substring bonus, with a number guard to prevent `History 2` matching `History 1`
5. **Country-indexed lookup** — `NO:` channels only search Norwegian EPG entries; cross-country false positives are eliminated

---

## Installation

1. In Dispatcharr, go to **Settings → Plugins**
2. Click **Import Plugin** in the top right
3. Upload `epg_suggester.zip`
4. Click **Enable** on the plugin card
5. Configure settings and click **Save**

Or copy files directly into your Dispatcharr data directory:
```bash
mkdir -p /data/plugins/epg_suggester
cp plugin.py plugin.json __init__.py /data/plugins/epg_suggester/
# Then reload plugins in the Dispatcharr UI
```

---

## Recommended Workflow

### 1 — Start with a preview
Click **📤 Export CSV** — this saves full results to `/data/exports/epg_suggester_TIMESTAMP.csv` without changing anything. Copy it out and inspect it:
```bash
docker cp dispatcharr:/data/exports/epg_suggester_TIMESTAMP.csv ./
```

### 2 — Apply in confidence tiers
- **Score 99-100** — callsign matches and perfect fuzzy matches. Safe to auto-apply all.
- **Score 90-98** — high confidence fuzzy matches. Review quickly, mostly correct.
- **Score 80-89** — moderate confidence. Manual review recommended before applying.
- **Below 80** — treat as suggestions only, verify each one manually in Dispatcharr.

### 3 — Apply
Set **Auto-Apply Min Score** in settings, enable **Auto-Apply**, then click **✅ Apply Best Suggestions**.

---

## Settings Reference

| Setting | Default | Description |
|---|---|---|
| 🎯 Minimum Match Score | `60` | Threshold to include a suggestion (0–100). Lower = more suggestions but less accurate. |
| 📋 Max Suggestions Per Channel | `3` | Top-N results shown per channel in CSV. |
| 📡 Limit to EPG Sources | *(all)* | Comma-separated EPG source names to search. Leave blank for all. |
| 📂 Limit to Channel Groups | *(all)* | Comma-separated channel group names to scan. Leave blank for all. |
| 🎬 Strip Quality Tags | `ON` | Removes `HD`, `4K`, `UHD` etc. before matching. |
| 🔧 Strip Misc Tags | `ON` | Removes `VIP`, `backup`, `+1` etc. before matching. |
| ⚡ Auto-Apply Best Match | `OFF` | When ON, Apply action will write EPG assignments. Always review CSV first! |
| 🔒 Auto-Apply Min Score | `85` | Safety floor — only matches at or above this score are assigned. |

---

## Actions

| Action | Description |
|---|---|
| 🔍 Scan & Suggest EPG | Runs the full matching engine and saves a text report to `/data/exports/`. Returns a preview in the UI. |
| 📤 Export Suggestions to CSV | Same as scan but saves results as a CSV for spreadsheet review. |
| ✅ Apply Best Suggestions | Assigns EPG to channels where the top suggestion meets the auto-apply threshold. Requires Auto-Apply to be enabled. |
| 📺 List Unmatched Channels | Fast list of all channels with no EPG assigned, grouped by channel group. No scoring performed. |

---

## EPG Sources

The plugin works against whatever EPG sources you have loaded in Dispatcharr. More sources = better coverage.

**Recommended free sources:**

General international coverage:
```
https://epg.pw/xmltv/epg_US.xml
https://epg.pw/xmltv/epg_GB.xml
```

US local affiliates (NBC/ABC/CBS/FOX) — includes callsign-based entries like `KSDK-DT`, `WCAU-DT`:
```
https://epgshare01.online/epgshare01/epg_ripper_ALL_SOURCES1.xml.gz
```

---

## How Callsign Matching Works

Many IPTV providers include the broadcast callsign in parentheses in the channel name:
```
PRIME: NBC ST. LOUIS NEWS (KSDK) ᴿᴬᵂ [FHD]
PRIME: CBS HARRISBURG (WHP) ᴿᴬᵂ [FHD]
US: FOX (KOKH) OKLAHOMA CITY HD
```

EPG sources like epgshare01 identify local stations by callsign:
```
KSDK-DT   →  NBC St. Louis
WHP-DT    →  CBS Harrisburg
KOKH-DT   →  FOX Oklahoma City
```

The plugin extracts the callsign from the channel name and looks it up directly in a pre-built callsign index — bypassing fuzzy matching entirely and returning score 100. This makes US local affiliate matching reliable even when the channel name and EPG entry share no common words.

---

## Country Code Awareness

The plugin distinguishes between country code prefixes and provider prefixes:

**Country codes** (kept for country-scoped matching):
`US`, `UK`, `GB`, `NO`, `SE`, `DK`, `FI`, `DE`, `FR`, `IT`, `ES`, `NL`, `PL`, `RO`, `HU`, `TR`, `RU`, `AR`, `CL`, and many more.

**Provider prefixes** (stripped before matching):
`GO:`, `NOW:`, `VIP:`, `PRIME:`, `SKY:`, `NBA:`, `MLB:`, `DSTV:`, `VO:`, `MXC:`, `WOW:` etc.

This means `NO: SF-KANALEN` will never incorrectly match `FI: SF-KANALEN`, but `GO: CNN` will correctly match `US: CNN 4K` (the `GO:` provider prefix is stripped, then `CNN` matches across all countries).

---

## Output Files

All output files are saved to `/data/exports/` inside the Dispatcharr container.

| File | Description |
|---|---|
| `epg_suggester_TIMESTAMP.csv` | Full suggestion results with scores |
| `epg_suggester_scan_TIMESTAMP.txt` | Human-readable scan report |

Copy a file out of the container:
```bash
docker cp dispatcharr:/data/exports/epg_suggester_TIMESTAMP.csv ./
```

### CSV columns

| Column | Description |
|---|---|
| `channel_id` | Dispatcharr internal channel ID |
| `channel_name` | Original channel name |
| `channel_norm` | Normalised name used for matching |
| `channel_group` | Channel group name |
| `rank` | Suggestion rank (1 = best) |
| `score` | Match confidence 0–100 |
| `epg_name` | Suggested EPG entry display name |
| `tvg_id` | EPG entry TVG ID |
| `epg_source` | EPG source name |
| `epg_data_id` | Dispatcharr internal EPG data ID |

---

## Performance

The plugin uses a word-inverted index and callsign index to avoid brute-force comparisons:

| EPG entries | Channels | Time |
|---|---|---|
| ~4,000 | ~2,000 | ~5s |
| ~20,000 | ~2,000 | ~10s |
| ~46,000 | ~1,500 | ~10s |

---

## Troubleshooting

**"No suggestions above threshold"**
Lower the Minimum Match Score to 50 and re-run. If still nothing, the channel name shares no words with any EPG entry — the EPG source likely doesn't cover that channel.

**Cross-country false positives**
If a country code is missing from the plugin's known list, open an issue with the code and it will be added.

**504 Gateway Time-out**
The scan still completes and saves results to `/data/exports/` even if the HTTP response times out. Check there for the output file.

**Score 100 but wrong match**
This is a callsign collision — two different stations with the same callsign in different markets. Manually correct the assignment in Dispatcharr's Channels page.

---

## License

MIT — free for personal and commercial use.

---

## Contributing

Pull requests welcome. Particularly useful contributions:
- Additional country codes for the prefix detection list
- Better handling of specific IPTV provider naming conventions
- Report false positives with channel name + EPG name so scoring can be improved
