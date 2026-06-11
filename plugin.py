import re as _re
import logging
import difflib

# ---------------------------------------------------------------------------
# Module-level compiled patterns — compiled once at import time for speed.
# ---------------------------------------------------------------------------

# Detects a normalised country prefix in the form "xx: " (e.g. "us: cnn")
_CTRY_RE     = _re.compile(r'^([a-z]{2,5}): ')
# Detects raw IPTV-style prefixes before the real channel name (e.g. "US| ", "UK: ", "FR-")
_PREFIX_RE   = _re.compile(r'^([A-Za-z]{2,5})\s*[|\-:]\s*')
# Lookalike Unicode letters (superscript, small-caps, etc.) that should map to spaces
_UNICODE_RE  = _re.compile(r'[ᴀ-ᶿⱠ-Ɀ⁰-₟²-³¹]+')
# Quality / resolution tags that add no matching value
_QUALITY_RE  = _re.compile(r'\b(?:4k|uhd|fhd|hd|sd|hevc|h265|h264|hdr|sdr|1080[pi]?|720[pi]?)\b', _re.IGNORECASE)
# Misc IPTV noise: tier labels, backup copies, parenthetical/bracketed suffixes, asterisks
_MISC_RE     = _re.compile(r'\b(?:vip|backup\d*|bkup|plus|premium|extra|alt|raw|\+1|\+2)\b|\([^)]{0,15}\)|\[[^\]]{0,15}\]|\s*\*+\s*', _re.IGNORECASE)
# Collapses any run of whitespace to a single space
_WS_RE       = _re.compile(r'\s+')
# Extracts a broadcast callsign embedded in channel names e.g. "(KSDK)" or "(KSDK-DT)"
_CALLSIGN_RE = _re.compile(r'\(([A-Z]{3,5}(?:-[A-Z0-9]+)?)\)')

# All known two-to-five letter ISO / regional codes used as IPTV channel prefixes
_COUNTRY_CODES = {
    'us','uk','gb','au','ca','de','fr','it','es','nl','be','ch','at',
    'no','se','dk','fi','pl','pt','ro','al','sr','hr','si','sk','cz',
    'hu','rs','ba','me','mk','bg','gr','tr','il','ar','br','mx','nz',
    'za','ie','is','lu','ee','lv','lt','ua','by','md','ge','am','az',
    'kz','uz','pk','in','sg','my','th','ph','id','jp','kr','cn','hk',
    'tw','ae','sa','qa','kw','bh','om','eg','ma','tn','dz','ly','ng',
    'ke','gh','tz','et','cm','ci','sn','rw','ug','ao','mz','ru','cl',
}

# Very common short words excluded from the word index — matching on them produces too many
# irrelevant candidates and slows down scoring.
_STOP_WORDS = {'hd','sd','tv','the','and','for','live','news','channel','network'}

# Roman numerals and number words mapped to digits for exact matching
_NUM_WORDS = {
    'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5',
    'six': '6', 'seven': '7', 'eight': '8', 'nine': '9', 'ten': '10',
    'i': '1', 'ii': '2', 'iii': '3', 'iv': '4', 'v': '5',
    'vi': '6', 'vii': '7', 'viii': '8', 'ix': '9', 'x': '10'
}

# Mappings of common country names/codes in group names / EPG source names to ISO codes
_COUNTRY_KEYWORDS = {
    "united states": "us", "usa": "us", "us": "us",
    "united kingdom": "gb", "uk": "gb", "gb": "gb", "great britain": "gb", "england": "gb", "british": "gb",
    "norway": "no", "norge": "no", "no": "no",
    "sweden": "se", "sverige": "se", "se": "se",
    "denmark": "dk", "danmark": "dk", "dk": "dk",
    "finland": "fi", "suomi": "fi", "fi": "fi",
    "germany": "de", "deutschland": "de", "de": "de", "deutsch": "de",
    "france": "fr", "fr": "fr", "french": "fr",
    "spain": "es", "espana": "es", "es": "es", "spanish": "es",
    "italy": "it", "italia": "it", "it": "it", "italian": "it",
    "netherlands": "nl", "nederland": "nl", "nl": "nl", "dutch": "nl",
    "poland": "pl", "polska": "pl", "pl": "pl",
    "portugal": "pt", "portuguese": "pt", "pt": "pt",
    "canada": "ca", "ca": "ca",
    "australia": "au", "au": "au",
}

# Single place to change the output directory used by all export/snapshot operations
_EXPORT_DIR = "/data/exports"


class Plugin:
    name        = "EPG Suggester"
    version     = "2.5.0"
    description = "Suggests EPG entries for channels without EPG assigned, using fuzzy name matching."

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self, action, params, context):
        """
        Entry point called by Dispatcharr for every plugin action.
        Parses settings, resolves the action name to a method, and returns
        either a string (displayed in the Dispatcharr UI) or an error dict.
        """
        log      = logging.getLogger("plugins.epg_suggester")
        settings = context.get("settings", {})
        cfg      = self._parse_settings(settings)
        log.info("EPG Suggester: action=%s", action)

        actions = {
            "show_unmatched":         self._show_unmatched,
            "scan_and_suggest":       self._scan,
            "export_suggestions_csv": self._export,
            "apply_suggestions":      self._apply,
            "dry_run_apply":          self._dry_run_apply,
            "restore_last_apply":     self._restore_last_apply,
            "audit_matched":          self._audit_matched,
            "show_stats":             self._show_stats,
            "apply_from_csv":         self._apply_from_csv,
        }
        fn = actions.get(action)
        if fn:
            return fn(cfg, log)
        return {"status": "error", "message": "Unknown action: " + action}

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _parse_settings(self, settings):
        """
        Convert the raw settings dict supplied by Dispatcharr into typed config values.
        Numeric fields are clamped to valid ranges. Fallbacks are used for missing/invalid keys.
        """
        def _int(key, default, lo, hi):
            try:
                val = settings.get(key)
                if val is None or val == "":
                    return default
                return max(lo, min(hi, int(val)))
            except (ValueError, TypeError):
                return default

        def _bool(key, default):
            val = settings.get(key)
            if val is None:
                return default
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                val_lower = val.lower().strip()
                if val_lower in ("true", "on", "yes", "1"):
                    return True
                if val_lower in ("false", "off", "no", "0"):
                    return False
            return bool(val)

        def _split_str(key):
            val = settings.get(key)
            if not val:
                return []
            if not isinstance(val, str):
                return []
            return [x.strip() for x in val.split(",") if x.strip()]

        return {
            "geo":    _bool("ignore_geo_prefixes", True),
            "qual":   _bool("ignore_quality_tags", True),
            "misc":   _bool("ignore_misc_tags", True),
            "min_s":  _int("min_score", 60, 0, 100),
            "max_n":  _int("max_suggestions", 3, 1, 10),
            "thresh": _int("auto_apply_threshold", 85, 0, 100),
            "sf":     _split_str("epg_sources_filter"),
            "gf":     _split_str("group_filter"),
            "auto":   _bool("auto_apply", False),
            "prio":   _split_str("preferred_sources"),
        }

    # ------------------------------------------------------------------
    # Name normalisation
    # ------------------------------------------------------------------

    @staticmethod
    def _norm(name, cfg):
        """
        Normalise a raw channel or EPG display name into a clean lowercase string
        suitable for comparison.
        """
        n = name.strip()
        n = _UNICODE_RE.sub(' ', n)
        if cfg["geo"]:
            m = _PREFIX_RE.match(n)
            if m:
                # Always strip the prefix from the normalized name for direct matching
                n = n[m.end():]
        if cfg["qual"]: n = _QUALITY_RE.sub(' ', n)
        if cfg["misc"]: n = _MISC_RE.sub(' ', n)
        
        # Pad special symbols with spaces for tokenized matching
        n = _re.sub(r'\s*([&+])\s*', r' \1 ', n)
        
        # Collapse whitespace and convert to lower
        n = _WS_RE.sub(' ', n).strip().lower()
        
        # Map number words and Roman numerals to digits
        words = []
        for word in n.split():
            words.append(_NUM_WORDS.get(word, word))
        return ' '.join(words)

    # ------------------------------------------------------------------
    # Country Detection & Scoring
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_countries(name, group_name="", source_name="", tvg_id=""):
        """
        Detect country codes from name prefixes/suffixes, channel group, EPG source, or tvg_id.
        """
        countries = set()
        
        # 1. Check name for prefixes/suffixes
        name_upper = (name or "").strip().upper()
        prefix_match = _PREFIX_RE.match(name_upper)
        if prefix_match:
            prefix = prefix_match.group(1).lower()
            if prefix in _COUNTRY_CODES:
                countries.add(prefix)
                if prefix == 'gb': countries.add('uk')
                if prefix == 'uk': countries.add('gb')
        
        # Suffix like "CNN US" or "CNN UK"
        words = name_upper.split()
        if words:
            last_word = words[-1].lower()
            last_word = _re.sub(r'[^a-z0-9]', '', last_word)
            if last_word in ['us', 'usa', 'uk', 'gb', 'no', 'se', 'dk', 'fi', 'de', 'fr', 'es', 'it', 'nl', 'pl', 'pt', 'ca', 'au']:
                c = last_word
                if c == 'usa': c = 'us'
                if c == 'gb': c = 'uk'
                countries.add(c)
                if c == 'uk': countries.add('gb')
                
        # 2. Check group_name (for channel)
        if group_name:
            group_lower = group_name.lower()
            for kw, code in _COUNTRY_KEYWORDS.items():
                if _re.search(r'\b' + _re.escape(kw) + r'\b', group_lower):
                    countries.add(code)
                    if code == 'gb': countries.add('uk')
                    if code == 'uk': countries.add('gb')
                    
        # 3. Check tvg_id (for EPG)
        if tvg_id:
            tvg_lower = tvg_id.lower()
            parts = tvg_lower.split('.')
            if len(parts) > 1:
                last_part = parts[-1]
                if last_part in _COUNTRY_CODES:
                    countries.add(last_part)
                    if last_part == 'gb': countries.add('uk')
                    if last_part == 'uk': countries.add('gb')
                for p in parts[:-1]:
                    if p in ['us', 'uk', 'gb', 'no', 'se', 'dk', 'fi', 'de', 'fr', 'es', 'it', 'nl', 'pl', 'pt', 'ca', 'au']:
                        countries.add(p)
                        if p == 'gb': countries.add('uk')
                        if p == 'uk': countries.add('gb')

        # 4. Check source_name (for EPG)
        if source_name:
            source_lower = source_name.lower()
            for kw, code in _COUNTRY_KEYWORDS.items():
                if _re.search(r'\b' + _re.escape(kw) + r'\b', source_lower):
                    countries.add(code)
                    if code == 'gb': countries.add('uk')
                    if code == 'uk': countries.add('gb')
                    
        return countries

    @staticmethod
    def _country_match_score(ch_countries, ep_countries):
        """
        Bonus for country alignment; heavy penalty for conflicting countries.
        """
        if not ch_countries or not ep_countries:
            return 0
        if ch_countries & ep_countries:
            return 10
        return -50

    # ------------------------------------------------------------------
    # Quality & Timeshift Scoring
    # ------------------------------------------------------------------

    @staticmethod
    def _quality_match_score(ch_raw, epg_raw):
        """
        Compare raw names for quality tags. Reward matches, penalize mismatches.
        """
        ch_raw = ch_raw.lower()
        epg_raw = epg_raw.lower()
        
        is_ch_hd = any(x in ch_raw for x in ["hd", "fhd", "1080", "720"])
        is_ep_hd = any(x in epg_raw for x in ["hd", "fhd", "1080", "720"])
        
        is_ch_uhd = any(x in ch_raw for x in ["4k", "uhd", "4kuhd"])
        is_ep_uhd = any(x in epg_raw for x in ["4k", "uhd", "4kuhd"])
        
        is_ch_sd = "sd" in ch_raw or (not is_ch_hd and not is_ch_uhd)
        is_ep_sd = "sd" in epg_raw or (not is_ep_hd and not is_ep_uhd)
        
        if is_ch_uhd and is_ep_uhd: return 3
        if is_ch_hd and is_ep_hd: return 2
        if is_ch_sd and is_ep_sd: return 1
        
        if is_ch_hd != is_ep_hd or is_ch_uhd != is_ep_uhd:
            return -5
        return 0

    @staticmethod
    def _timeshift_match_score(ch_raw, epg_raw):
        """
        Compare raw names for timeshift indicators (+1, +2).
        """
        ch_raw = ch_raw.lower()
        epg_raw = epg_raw.lower()
        ch_ts = any(x in ch_raw for x in ["+1", "plus 1", "+ 1"])
        ep_ts = any(x in epg_raw for x in ["+1", "plus 1", "+ 1"])
        if ch_ts == ep_ts:
            return 2
        return -5

    # ------------------------------------------------------------------
    # Scoring Algorithm
    # ------------------------------------------------------------------

    @staticmethod
    def _fast_score(ct, cs, cn, et, es, en, min_s):
        """
        Compute a similarity score (0-99) between a channel and an EPG entry.
        """
        if cn == en: return 100
        
        # Number check
        ch_nums = set(t for t in ct if t.isdigit())
        ep_nums = set(t for t in et if t.isdigit())
        number_penalty = 0
        if ch_nums or ep_nums:
            if ch_nums & ep_nums:
                pass
            elif ch_nums and ep_nums:
                # Hard mismatch
                return 0
            else:
                # Soft mismatch
                number_penalty = -25

        inter     = len(cs & es) if cs and es else 0
        union     = max(len(cs), len(es)) if (cs or es) else 1
        overlap_s = int(inter / union * 90)
        sub       = 20 if (cn in en or en in cn) else 0
        
        if overlap_s + sub + number_penalty < min_s:
            return max(0, overlap_s + sub + number_penalty)
            
        if overlap_s >= 40 or sub:
            ratio     = difflib.SequenceMatcher(None, ' '.join(sorted(ct)), ' '.join(sorted(et))).ratio()
            overlap_s = max(overlap_s, int(ratio * 90))
            
        return max(0, min(99, overlap_s + sub + number_penalty))

    # ------------------------------------------------------------------
    # Index construction
    # ------------------------------------------------------------------

    def _build_index(self, epg_entries, cfg):
        """
        Build fast lookup structures from the list of EPG entries.
        """
        by_country = {}
        no_country = []
        word_index = {}

        for e in epg_entries:
            raw = (e["name"] or "").strip()
            if not raw:
                continue
            norm = self._norm(raw, cfg)
            tok  = norm.split()
            tset = set(tok)
            
            # Detect countries
            epg_source = e["epg_source__name"] or ""
            tvg_id = e["tvg_id"] or ""
            countries = self._detect_countries(raw, source_name=epg_source, tvg_id=tvg_id)
            
            # Callsign extraction
            raw_upper = raw.strip().upper()
            prefix_match = _PREFIX_RE.match(raw_upper)
            clean_upper = raw_upper[prefix_match.end():].strip() if prefix_match else raw_upper
            cs_match = _re.match(r'^([A-Z]{3,5})\b', clean_upper)
            epg_callsign = cs_match.group(1) if cs_match else ''
            
            entry = {
                "id":           e["id"],
                "name":         raw,
                "tvg_id":       tvg_id,
                "source":       epg_source,
                "norm":         norm,
                "tok":          tok,
                "tset":         tset,
                "epg_callsign": epg_callsign,
                "countries":    countries,
            }
            
            if countries:
                for c in countries:
                    by_country.setdefault(c, []).append(entry)
            else:
                no_country.append(entry)
                
            for word in tset:
                if len(word) >= 3 and word not in _STOP_WORDS:
                    word_index.setdefault(word, []).append(entry)

        # Build callsign index across all entries
        callsign_index = {}
        seen_callsign_eids = set()
        for entries_list in [no_country] + list(by_country.values()):
            for entry in entries_list:
                eid = entry["id"]
                if eid in seen_callsign_eids:
                    continue
                seen_callsign_eids.add(eid)
                cs = entry.get("epg_callsign", "")
                if cs:
                    callsign_index.setdefault(cs, []).append(entry)

        return by_country, no_country, word_index, callsign_index

    # ------------------------------------------------------------------
    # Candidate retrieval
    # ------------------------------------------------------------------

    def _candidates_for(self, ch_countries, ch_tok, ch_set, by_country, no_country, word_index):
        """
        Return a deduplicated list of EPG entries worth scoring against this channel.
        """
        candidates = []
        seen = set()
        
        # 1. Pull from matching country buckets
        if ch_countries:
            for c in ch_countries:
                for entry in by_country.get(c, []):
                    eid = entry["id"]
                    if eid not in seen:
                        seen.add(eid)
                        candidates.append(entry)
        
        # 2. Pull from word index (skipping country conflicts)
        meaningful = [w for w in ch_tok if len(w) >= 3 and w not in _STOP_WORDS and not w.isdigit()]
        if meaningful:
            for word in meaningful:
                for entry in word_index.get(word, []):
                    if ch_countries and entry["countries"] and not (ch_countries & entry["countries"]):
                        continue
                    eid = entry["id"]
                    if eid not in seen:
                        seen.add(eid)
                        candidates.append(entry)
        else:
            for entry in no_country:
                eid = entry["id"]
                if eid not in seen:
                    seen.add(eid)
                    candidates.append(entry)
            if not ch_countries:
                for country_list in by_country.values():
                    for entry in country_list:
                        eid = entry["id"]
                        if eid not in seen:
                            seen.add(eid)
                            candidates.append(entry)
                            
        return candidates

    # ------------------------------------------------------------------
    # Suggestion engine
    # ------------------------------------------------------------------

    def _suggest(self, ch_norm, ch_raw, ch_group, by_country, no_country, word_index, callsign_index, cfg):
        """
        Score all candidates for a single channel and return the top-N matches.
        """
        ct    = ch_norm.split()
        cs    = set(ct)
        min_s = cfg["min_s"]

        # Detect countries for channel
        ch_countries = self._detect_countries(ch_raw, group_name=ch_group)

        # Extract callsign embedded in the raw channel name
        ch_callsign = ''
        cm = _CALLSIGN_RE.search(ch_raw)
        if cm:
            ch_callsign = cm.group(1).upper()
            ch_callsign = _re.sub(r'[-.].*$', '', ch_callsign)
        else:
            # Fallback: check if any token matches an EPG callsign
            for token in ct:
                token_upper = token.upper()
                if len(token_upper) >= 3 and token_upper in callsign_index:
                    if token_upper.lower() not in _COUNTRY_CODES and token_upper.lower() not in _STOP_WORDS:
                        ch_callsign = token_upper
                        break

        candidates = self._candidates_for(ch_countries, ct, cs, by_country, no_country, word_index)
        if ch_callsign:
            candidates = candidates + callsign_index.get(ch_callsign, [])

        prio_map   = {s: i for i, s in enumerate(cfg["prio"])} if cfg["prio"] else {}
        prio_worst = len(cfg["prio"])
        scored     = []
        seen_ids   = set()

        for e in candidates:
            eid = e["id"]
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            
            if ch_callsign and e.get("epg_callsign") == ch_callsign:
                base_score = 100
                match_type = "callsign"
            else:
                base_score = self._fast_score(ct, cs, ch_norm, e["tok"], e["tset"], e["norm"], min_s)
                match_type = "fuzzy"
                
            if base_score >= min_s:
                country_adj = self._country_match_score(ch_countries, e["countries"])
                quality_adj = self._quality_match_score(ch_raw, e["name"])
                timeshift_adj = self._timeshift_match_score(ch_raw, e["name"])
                
                final_score = base_score + country_adj + quality_adj + timeshift_adj
                final_score = max(0, min(100, final_score))
                
                if final_score >= min_s:
                    scored.append((final_score, prio_map.get(e["source"], prio_worst), e, match_type))

        scored.sort(key=lambda x: (-x[0], x[1]))

        result = []
        for s, _, e, match_type in scored:
            result.append(dict(e, score=s, match_type=match_type))
            if len(result) >= cfg["max_n"]:
                break
        return result

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _get_channels(self, cfg, log, matched=False, order_by=None):
        """
        Fetch channels from the database.
        """
        from apps.channels.models import Channel
        qs = Channel.objects.select_related("channel_group").filter(
            epg_data__isnull=(not matched)
        )
        if cfg["gf"]:
            qs = qs.filter(channel_group__name__in=cfg["gf"])
        if order_by:
            qs = qs.order_by(*order_by)
        if matched:
            channels = list(qs.values("id", "name", "channel_group__name", "epg_data_id", "epg_data__name"))
        else:
            channels = list(qs.values("id", "name", "channel_group__name"))
        log.info("EPG Suggester: %d channels fetched (matched=%s)", len(channels), matched)
        return channels

    def _get_epg(self, cfg, log):
        """
        Fetch EPG entries from the database.
        """
        from apps.epg.models import EPGData
        qs = EPGData.objects.select_related("epg_source").values(
            "id", "name", "tvg_id", "epg_source__name"
        )
        if cfg["sf"]:
            qs = qs.filter(epg_source__name__in=cfg["sf"])
        entries = list(qs)
        log.info("EPG Suggester: %d EPG entries fetched", len(entries))
        return entries

    # ------------------------------------------------------------------
    # Core pipeline
    # ------------------------------------------------------------------

    def _run_matching(self, cfg, log):
        """
        Full matching pipeline: fetch unmatched channels + EPG data, build the index,
        then run _suggest for every channel.
        """
        channels                                            = self._get_channels(cfg, log)
        epg_raw                                             = self._get_epg(cfg, log)
        by_country, no_country, word_index, callsign_index = self._build_index(epg_raw, cfg)
        log.info(
            "EPG Suggester: index built (%d country groups, %d no-prefix, %d word-index keys, %d callsigns)",
            len(by_country), len(no_country), len(word_index), len(callsign_index),
        )
        results = []
        for ch in channels:
            raw   = ch["name"] or ""
            group = ch.get("channel_group__name") or ""
            norm  = self._norm(raw, cfg)
            sugg  = self._suggest(norm, raw, group, by_country, no_country, word_index, callsign_index, cfg)
            results.append({
                "channel_id":    ch["id"],
                "channel_name":  raw,
                "channel_norm":  norm,
                "channel_group": group,
                "suggestions":   sugg,
            })
        matched = sum(1 for r in results if r["suggestions"])
        log.info("EPG Suggester: matching done. %d/%d channels matched", matched, len(results))
        return results

    # ------------------------------------------------------------------
    # Rollback helper (shared by _apply and _apply_from_csv)
    # ------------------------------------------------------------------

    def _save_rollback(self, channel_ids, log):
        """
        Snapshot the current epg_data_id values for the given channel IDs.
        """
        import json, os
        from datetime import datetime
        from apps.channels.models import Channel
        snapshot = list(Channel.objects.filter(pk__in=channel_ids).values("id", "epg_data_id"))
        os.makedirs(_EXPORT_DIR, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = _EXPORT_DIR + "/epg_suggester_rollback_" + ts + ".json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f)
        log.info("EPG Suggester: rollback snapshot -> %s (%d channels)", path, len(snapshot))
        return path

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _show_unmatched(self, cfg, log):
        """
        List all channels that currently have no EPG assigned, grouped by channel group.
        """
        channels = self._get_channels(cfg, log, order_by=["channel_group__name", "name"])
        if not channels:
            return "All channels already have EPG assigned!"
        lines = [str(len(channels)) + " channels without EPG:\n"]
        grp   = None
        for c in channels:
            g = c.get("channel_group__name") or "No Group"
            if g != grp:
                lines.append("\n[" + g + "]")
                grp = g
            lines.append("  id=" + str(c["id"]) + "  " + (c["name"] or ""))
        return "\n".join(lines)

    def _scan(self, cfg, log):
        """
        Run the full matching pipeline and write a human-readable report to _EXPORT_DIR.
        """
        import os
        from datetime import datetime
        results = self._run_matching(cfg, log)
        matched = sum(1 for r in results if r["suggestions"])
        lines   = [
            "EPG Suggester v" + self.version + " - Scan Results",
            str(len(results)) + " unmatched  |  suggestions found: " + str(matched),
            "",
        ]
        for r in results:
            lines.append("---")
            lines.append("Channel: " + r["channel_name"] + "  [" + r["channel_group"] + "]")
            lines.append("  norm: " + r["channel_norm"])
            if r["suggestions"]:
                for i, s in enumerate(r["suggestions"], 1):
                    lines.append(
                        "  [" + str(i) + "] score=" + str(s["score"])
                        + " (" + s.get("match_type", "fuzzy") + ")"
                        + "  " + s["name"]
                        + "  source=" + s["source"]
                        + "  id=" + str(s["id"])
                    )
            else:
                lines.append("  No suggestions above score " + str(cfg["min_s"]))
        os.makedirs(_EXPORT_DIR, exist_ok=True)
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = _EXPORT_DIR + "/epg_suggester_scan_" + ts + ".txt"
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        log.info("EPG Suggester: scan saved to %s", out)
        preview = "\n".join(lines[:60])
        if len(lines) > 60:
            preview += "\n\n... full results in " + out
        return preview

    def _export(self, cfg, log):
        """
        Run the matching pipeline and save all results to a timestamped CSV in _EXPORT_DIR.
        """
        import csv, os
        from datetime import datetime
        results = self._run_matching(cfg, log)
        matched = sum(1 for r in results if r["suggestions"])
        os.makedirs(_EXPORT_DIR, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = _EXPORT_DIR + "/epg_suggester_" + ts + ".csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write("# EPG Suggester v" + self.version + " | " + datetime.now().isoformat() + "\n")
            fh.write("# min_score=" + str(cfg["min_s"]) + "  max_suggestions=" + str(cfg["max_n"]) + "\n#\n")
            w = csv.writer(fh)
            w.writerow(["channel_id", "channel_name", "channel_norm", "channel_group",
                        "rank", "score", "match_type", "epg_name", "tvg_id", "epg_source", "epg_data_id"])
            for r in results:
                if r["suggestions"]:
                    for rank, s in enumerate(r["suggestions"], 1):
                        w.writerow([
                            r["channel_id"], r["channel_name"], r["channel_norm"], r["channel_group"],
                            rank, s["score"], s.get("match_type", "fuzzy"),
                            s["name"], s["tvg_id"], s["source"], s["id"],
                        ])
                else:
                    w.writerow([r["channel_id"], r["channel_name"], r["channel_norm"],
                                r["channel_group"], "", "", "", "NO_MATCH", "", "", ""])
        log.info("EPG Suggester: CSV saved to %s  (%d/%d matched)", path, matched, len(results))
        return (
            "CSV saved to " + path
            + "\nMatched: " + str(matched) + " / " + str(len(results))
            + "\n\ndocker cp dispatcharr:" + path + " ./"
        )

    def _apply(self, cfg, log):
        """
        Write EPG assignments to the database for every unmatched channel whose top
        suggestion meets or exceeds the auto-apply threshold.
        """
        from apps.channels.models import Channel
        from django.db import transaction
        
        if not cfg["auto"]:
            return "Auto-Apply is DISABLED. Enable it in settings first."
        results  = self._run_matching(cfg, log)
        to_apply = [
            r for r in results
            if r["suggestions"] and r["suggestions"][0]["score"] >= cfg["thresh"]
        ]
        if not to_apply:
            return "No suggestions met the threshold of " + str(cfg["thresh"]) + ". Nothing applied."

        rollback_path = self._save_rollback([r["channel_id"] for r in to_apply], log)
        applied = failed = 0
        skipped = len(results) - len(to_apply)

        try:
            with transaction.atomic():
                for r in to_apply:
                    top = r["suggestions"][0]
                    try:
                        Channel.objects.filter(pk=r["channel_id"]).update(epg_data_id=top["id"])
                        log.info("EPG Suggester: APPLY  %s -> %s (score=%d)",
                                 r["channel_name"], top["name"], top["score"])
                        applied += 1
                    except Exception as e:
                        log.error("EPG Suggester: FAIL  %s -> %s", r["channel_name"], e)
                        failed += 1
        except Exception as tx_err:
            log.error("EPG Suggester: Transaction error during APPLY: %s", tx_err)
            return "Transaction failed during apply: " + str(tx_err)

        return (
            "Applied: " + str(applied)
            + "  Skipped: " + str(skipped)
            + "  Failed: " + str(failed)
            + "\nRollback saved to: " + rollback_path
        )

    def _dry_run_apply(self, cfg, log):
        """
        Preview exactly what 'apply_suggestions' would write to the database.
        """
        results     = self._run_matching(cfg, log)
        would_apply = [
            r for r in results
            if r["suggestions"] and r["suggestions"][0]["score"] >= cfg["thresh"]
        ]
        would_skip  = len(results) - len(would_apply)
        lines = [
            "EPG Suggester v" + self.version + " - Dry Run (no changes written)",
            "Threshold: " + str(cfg["thresh"])
            + "  |  Would apply: " + str(len(would_apply))
            + "  |  Would skip: " + str(would_skip),
            "",
        ]
        for r in would_apply:
            top = r["suggestions"][0]
            lines.append("  " + r["channel_name"] + "  [" + r["channel_group"] + "]")
            lines.append(
                "    -> " + top["name"]
                + "  (score=" + str(top["score"])
                + ", " + top.get("match_type", "fuzzy")
                + ", source=" + top["source"] + ")"
            )
        return "\n".join(lines)

    def _restore_last_apply(self, cfg, log):
        """
        Revert EPG assignments to the state captured by the most recent rollback snapshot.
        """
        import json, glob, os
        from apps.channels.models import Channel
        from django.db import transaction
        
        files = sorted(glob.glob(_EXPORT_DIR + "/epg_suggester_rollback_*.json"), reverse=True)
        if not files:
            return "No rollback snapshot found in " + _EXPORT_DIR + "."
        latest = files[0]
        with open(latest, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
        restored = failed = 0
        
        try:
            with transaction.atomic():
                for entry in snapshot:
                    try:
                        Channel.objects.filter(pk=entry["id"]).update(epg_data_id=entry["epg_data_id"])
                        restored += 1
                    except Exception as e:
                        log.error("EPG Suggester: RESTORE FAIL  channel_id=%s -> %s", entry["id"], e)
                        failed += 1
        except Exception as tx_err:
            log.error("EPG Suggester: Transaction error during RESTORE: %s", tx_err)
            return "Transaction failed during restore: " + str(tx_err)
            
        log.info("EPG Suggester: restored %d channels from %s", restored, os.path.basename(latest))
        return (
            "Restored " + str(restored) + " channels from " + os.path.basename(latest)
            + ".  Failed: " + str(failed)
        )

    def _audit_matched(self, cfg, log):
        """
        Scan channels that already have EPG assigned and flag any where a better match
        exists in the current EPG data.
        """
        channels                                            = self._get_channels(cfg, log, matched=True)
        epg_raw                                             = self._get_epg(cfg, log)
        by_country, no_country, word_index, callsign_index = self._build_index(epg_raw, cfg)
        flagged = []
        for ch in channels:
            raw   = ch["name"] or ""
            group = ch.get("channel_group__name") or ""
            norm  = self._norm(raw, cfg)
            sugg  = self._suggest(norm, raw, group, by_country, no_country, word_index, callsign_index, cfg)
            if sugg:
                top = sugg[0]
                if top["id"] != ch["epg_data_id"] and top["score"] >= cfg["thresh"]:
                    flagged.append({
                        "channel":    raw,
                        "group":      group,
                        "current":    ch.get("epg_data__name") or ("id=" + str(ch["epg_data_id"])),
                        "suggested":  top["name"],
                        "score":      top["score"],
                        "source":     top["source"],
                        "match_type": top.get("match_type", "fuzzy"),
                    })
        if not flagged:
            return "No better matches found for already-assigned channels."
        lines = [str(len(flagged)) + " channels may have a better EPG match:\n"]
        for fl in flagged:
            lines.append("  " + fl["channel"] + "  [" + fl["group"] + "]")
            lines.append("    Current:   " + fl["current"])
            lines.append(
                "    Suggested: " + fl["suggested"]
                + "  (score=" + str(fl["score"])
                + ", " + fl["match_type"]
                + ", source=" + fl["source"] + ")"
            )
            lines.append("")
        return "\n".join(lines)

    def _show_stats(self, cfg, log):
        """
        Return a quick statistics overview without running the matching engine.
        """
        from apps.channels.models import Channel
        from apps.epg.models import EPGData
        from django.db.models import Count
        total     = Channel.objects.count()
        matched   = Channel.objects.filter(epg_data__isnull=False).count()
        unmatched = total - matched
        epg_total = EPGData.objects.count()
        groups    = (Channel.objects
                     .values("channel_group__name")
                     .annotate(total=Count("id"), matched=Count("epg_data"))
                     .order_by("channel_group__name"))
        lines = [
            "EPG Suggester v" + self.version + " - Statistics",
            "",
            "Channels  : " + str(total) + " total  |  " + str(matched) + " matched  |  " + str(unmatched) + " unmatched",
            "EPG Entries: " + str(epg_total),
            "",
            "  {:<30} {:>6} {:>8} {:>10}".format("Group", "Total", "Matched", "Unmatched"),
            "  " + "-" * 56,
        ]
        for g in groups:
            name = (g["channel_group__name"] or "No Group")[:30]
            t    = g["total"]
            m    = g["matched"]
            lines.append("  {:<30} {:>6} {:>8} {:>10}".format(name, t, m, t - m))
        return "\n".join(lines)

    def _apply_from_csv(self, cfg, log):
        """
        Apply EPG assignments from the most recently exported CSV file.
        """
        import csv, glob, os
        from apps.channels.models import Channel
        from django.db import transaction
        
        # Match only timestamped export files (YYYYMMDD_...), not scan txt files or rollbacks
        files = sorted(glob.glob(_EXPORT_DIR + "/epg_suggester_[0-9]*.csv"), reverse=True)
        if not files:
            return "No EPG Suggester CSV export found in " + _EXPORT_DIR + "."
        latest   = files[0]
        to_apply = []
        
        # Open with utf-8-sig to automatically handle UTF-8 BOM from Excel
        with open(latest, "r", encoding="utf-8-sig") as fh:
            lines = [line for line in fh if not line.startswith("#")]
            
        # Detect delimiter automatically (comma or semicolon)
        delimiter = ','
        if lines:
            header = lines[0]
            if ';' in header and ',' not in header:
                delimiter = ';'
            elif ';' in header and ',' in header:
                if header.count(';') > header.count(','):
                    delimiter = ';'
                    
        for row in csv.DictReader(lines, delimiter=delimiter):
            if row.get("epg_name") == "NO_MATCH":
                continue
            try:
                if int(row.get("rank") or 0) != 1:
                    continue
                to_apply.append({
                    "channel_id":   int(row["channel_id"]),
                    "epg_data_id":  int(row["epg_data_id"]),
                    "channel_name": row.get("channel_name", ""),
                    "epg_name":     row.get("epg_name", ""),
                })
            except (KeyError, ValueError):
                continue
        if not to_apply:
            return "No applicable rank=1 rows found in " + os.path.basename(latest) + "."
        rollback_path = self._save_rollback([r["channel_id"] for r in to_apply], log)
        applied = failed = 0
        
        try:
            with transaction.atomic():
                for r in to_apply:
                    try:
                        Channel.objects.filter(pk=r["channel_id"]).update(epg_data_id=r["epg_data_id"])
                        log.info("EPG Suggester: CSV APPLY  %s -> %s", r["channel_name"], r["epg_name"])
                        applied += 1
                    except Exception as e:
                        log.error("EPG Suggester: CSV APPLY FAIL  %s -> %s", r["channel_name"], e)
                        failed += 1
        except Exception as tx_err:
            log.error("EPG Suggester: Transaction error during CSV APPLY: %s", tx_err)
            return "Transaction failed during CSV apply: " + str(tx_err)
            
        return (
            "Applied " + str(applied) + " assignments from " + os.path.basename(latest)
            + ".  Failed: " + str(failed)
            + "\nRollback saved to: " + rollback_path
        )
