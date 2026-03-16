import re as _re

_CTRY_RE    = _re.compile(r'^([a-z]{2,5}): ')
_PREFIX_RE  = _re.compile(r'^([A-Za-z]{2,5})\s*[|\-:]\s*')
_UNICODE_RE = _re.compile(r'[\u1d00-\u1dbf\u2c60-\u2c7f\u2070-\u209f\u00b2-\u00b3\u00b9]+')
_QUALITY_RE = _re.compile(r'\b(?:4k|uhd|fhd|hd|sd|hevc|h265|h264|hdr|sdr|1080[pi]?|720[pi]?)\b', _re.IGNORECASE)
_MISC_RE    = _re.compile(r'\b(?:vip|backup\d*|bkup|plus|premium|extra|alt|raw|\+1|\+2)\b|\([^)]{0,15}\)|\[[^\]]{0,15}\]|\s*\*+\s*', _re.IGNORECASE)
_WS_RE      = _re.compile(r'\s+')

_COUNTRY_CODES = {
    'us','uk','gb','au','ca','de','fr','it','es','nl','be','ch','at',
    'no','se','dk','fi','pl','pt','ro','al','sr','hr','si','sk','cz',
    'hu','rs','ba','me','mk','bg','gr','tr','il','ar','br','mx','nz',
    'za','ie','is','lu','ee','lv','lt','ua','by','md','ge','am','az',
    'kz','uz','pk','in','sg','my','th','ph','id','jp','kr','cn','hk',
    'tw','ae','sa','qa','kw','bh','om','eg','ma','tn','dz','ly','ng',
    'ke','gh','tz','et','cm','ci','sn','rw','ug','ao','mz','ru','cl',
}

# Short common words to exclude from the word index (too many false hits)
_STOP_WORDS = {'hd','sd','tv','the','and','for','live','news','channel','network'}
_CALLSIGN_RE = _re.compile(r'\(([A-Z]{2,5}(?:-[A-Z0-9]+)?)\)')


class Plugin:
    name        = "EPG Suggester"
    version     = "2.3.0"
    description = "Suggests EPG entries for channels without EPG assigned, using fuzzy name matching."

    def run(self, action, params, context):
        import logging
        log      = logging.getLogger("plugins.epg_suggester")
        settings = context.get("settings", {})
        cfg      = self._parse_settings(settings)
        log.info("EPG Suggester: action=%s", action)

        if action == "show_unmatched":      return self._show_unmatched(cfg, log)
        elif action == "scan_and_suggest":  return self._scan(cfg, log)
        elif action == "export_suggestions_csv": return self._export(cfg, log)
        elif action == "apply_suggestions": return self._apply(cfg, log)
        else: return {"status": "error", "message": "Unknown action: " + action}

    def _parse_settings(self, settings):
        return {
            "qual":   bool(settings.get("ignore_quality_tags", True)),
            "misc":   bool(settings.get("ignore_misc_tags", True)),
            "min_s":  max(0, min(100, int(settings.get("min_score", 60)))),
            "max_n":  max(1, min(10,  int(settings.get("max_suggestions", 3)))),
            "sf":     [x.strip() for x in settings.get("epg_sources_filter", "").split(",") if x.strip()],
            "gf":     [x.strip() for x in settings.get("group_filter", "").split(",") if x.strip()],
            "auto":   bool(settings.get("auto_apply", False)),
            "thresh": max(0, min(100, int(settings.get("auto_apply_threshold", 85)))),
        }

    @staticmethod
    def _norm(name, cfg):
        n = name.strip()
        n = _UNICODE_RE.sub(' ', n)
        m = _PREFIX_RE.match(n)
        if m:
            prefix = m.group(1).lower()
            rest   = n[m.end():]
            n = (prefix + ': ' + rest) if prefix in _COUNTRY_CODES else rest
        if cfg["qual"]: n = _QUALITY_RE.sub(' ', n)
        if cfg["misc"]: n = _MISC_RE.sub(' ', n)
        return _WS_RE.sub(' ', n).strip().lower()

    @staticmethod
    def _fast_score(ct, cs, cn, et, es, en, min_s):
        import difflib
        if cn == en: return 100
        ch_nums = set(t for t in ct if t.isdigit())
        ep_nums = set(t for t in et if t.isdigit())
        if ch_nums and ep_nums and not (ch_nums & ep_nums):
            return 0
        inter     = len(cs & es) if cs and es else 0
        union     = max(len(cs), len(es)) if (cs or es) else 1
        overlap_s = int(inter / union * 90)
        sub       = 20 if (cn in en or en in cn) else 0
        if overlap_s + sub < min_s:
            return overlap_s + sub
        if overlap_s >= 40 or sub:
            ratio     = difflib.SequenceMatcher(None, ' '.join(sorted(ct)), ' '.join(sorted(et))).ratio()
            overlap_s = max(overlap_s, int(ratio * 90))
        return min(99, overlap_s + sub)

    def _build_index(self, epg_entries, cfg):
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
            # Extract callsign from EPG display name: "KSDK-DT" -> "KSDK"
            raw_upper = raw.strip().upper()
            cs_match = _re.match(r'^([A-Z]{2,5})(?:[-.]|$)', raw_upper)
            epg_callsign = cs_match.group(1) if cs_match and _re.match(r'^[A-Z]{2,5}$', cs_match.group(1)) else ''
            entry = {
                "id":           e["id"],
                "name":         raw,
                "tvg_id":       e["tvg_id"] or "",
                "source":       e["epg_source__name"] or "",
                "norm":         norm,
                "tok":          tok,
                "tset":         tset,
                "epg_callsign": epg_callsign,
            }
            m = _CTRY_RE.match(norm)
            if m:
                by_country.setdefault(m.group(1), []).append(entry)
            else:
                no_country.append(entry)
                for word in tset:
                    if len(word) >= 3 and word not in _STOP_WORDS:
                        word_index.setdefault(word, []).append(entry)

        # Build callsign index from raw EPG names
        callsign_index = {}
        for entries in [no_country] + list(by_country.values()):
            for entry in entries:
                cs = entry.get("epg_callsign", "")
                if cs:
                    callsign_index.setdefault(cs, []).append(entry)

        return by_country, no_country, word_index, callsign_index


    def _candidates_for(self, ch_norm, ch_tok, ch_set, by_country, no_country, word_index):
        """Return candidate EPG entries for this channel using word-index for no-prefix entries."""
        m = _CTRY_RE.match(ch_norm)

        # Country-prefixed entries: exact bucket lookup (fast)
        country_entries = by_country.get(m.group(1), []) if m else []

        # No-prefix entries: use word-index to find candidates sharing >= 1 meaningful word
        meaningful = [w for w in ch_tok if len(w) >= 3 and w not in _STOP_WORDS and not w.isdigit()]
        if meaningful:
            seen = set()
            nc_candidates = []
            for word in meaningful:
                for entry in word_index.get(word, []):
                    eid = entry["id"]
                    if eid not in seen:
                        seen.add(eid)
                        nc_candidates.append(entry)
        else:
            # No meaningful words — fall back to full no_country scan
            nc_candidates = no_country

        # If no country prefix on channel, also search all country buckets via word-index
        if not m:
            # Already have nc_candidates; add country-bucket hits via word-index
            # (word_index only covers no_country; for country buckets do a small linear scan
            #  but only buckets whose name contains a meaningful word)
            seen_c = set()
            for word in meaningful:
                for country_list in by_country.values():
                    for entry in country_list:
                        if word in entry["tset"] and entry["id"] not in seen_c:
                            seen_c.add(entry["id"])
                            nc_candidates.append(entry)

        return country_entries + nc_candidates

    def _suggest(self, ch_norm, ch_raw, by_country, no_country, word_index, callsign_index, cfg):
        ct    = ch_norm.split()
        cs    = set(ct)
        min_s = cfg["min_s"]

        # Extract callsign from raw channel name e.g. "PRIME: NBC ST LOUIS (KSDK) RAW"
        ch_callsign = ''
        cm = _CALLSIGN_RE.search(ch_raw)
        if cm:
            ch_callsign = cm.group(1).upper()
            # Also strip -DT/-CD suffixes for comparison
            ch_callsign = _re.sub(r'[-.].*$', '', ch_callsign)

        candidates = self._candidates_for(ch_norm, ct, cs, by_country, no_country, word_index)

        # Callsign direct lookup - O(1) using pre-built index
        if ch_callsign:
            cs_hits = callsign_index.get(ch_callsign, [])
            candidates = candidates + cs_hits

        scored = []
        seen_ids = set()
        for e in candidates:
            eid = e["id"]
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            # Callsign exact match = score 100
            if ch_callsign and e.get("epg_callsign") == ch_callsign:
                scored.append((100, e))
            else:
                s = self._fast_score(ct, cs, ch_norm, e["tok"], e["tset"], e["norm"], min_s)
                if s >= min_s:
                    scored.append((s, e))
        scored.sort(key=lambda x: x[0], reverse=True)

        # Deduplicate by id
        seen, result = set(), []
        for s, e in scored:
            if e["id"] not in seen:
                seen.add(e["id"])
                result.append(dict(e, score=s))
            if len(result) >= cfg["max_n"]:
                break
        return result

    def _get_channels(self, cfg, log):
        from apps.channels.models import Channel
        qs = Channel.objects.select_related("channel_group").filter(epg_data__isnull=True)
        if cfg["gf"]:
            qs = qs.filter(channel_group__name__in=cfg["gf"])
        channels = list(qs.values("id", "name", "channel_group__name"))
        log.info("EPG Suggester: %d unmatched channels", len(channels))
        return channels

    def _get_epg(self, cfg, log):
        from apps.epg.models import EPGData
        qs = EPGData.objects.select_related("epg_source").values("id", "name", "tvg_id", "epg_source__name")
        if cfg["sf"]:
            qs = qs.filter(epg_source__name__in=cfg["sf"])
        entries = list(qs)
        log.info("EPG Suggester: %d EPG entries fetched", len(entries))
        return entries

    def _run_matching(self, cfg, log):
        channels               = self._get_channels(cfg, log)
        epg_raw                = self._get_epg(cfg, log)
        by_country, no_country, word_index, callsign_index = self._build_index(epg_raw, cfg)
        log.info("EPG Suggester: index built (%d country groups, %d no-prefix, %d word-index keys, %d callsigns)",
                 len(by_country), len(no_country), len(word_index), len(callsign_index))
        results = []
        for ch in channels:
            raw  = ch["name"] or ""
            norm = self._norm(raw, cfg)
            sugg = self._suggest(norm, raw, by_country, no_country, word_index, callsign_index, cfg)
            results.append({
                "channel_id":    ch["id"],
                "channel_name":  raw,
                "channel_norm":  norm,
                "channel_group": ch.get("channel_group__name") or "",
                "suggestions":   sugg,
            })
        matched = sum(1 for r in results if r["suggestions"])
        log.info("EPG Suggester: matching done. %d/%d matched", matched, len(results))
        return results

    def _show_unmatched(self, cfg, log):
        from apps.channels.models import Channel
        qs = Channel.objects.select_related("channel_group").filter(epg_data__isnull=True)
        if cfg["gf"]:
            qs = qs.filter(channel_group__name__in=cfg["gf"])
        channels = list(qs.values("id", "name", "channel_group__name").order_by("channel_group__name", "name"))
        if not channels:
            return "All channels already have EPG assigned!"
        lines = [str(len(channels)) + " channels without EPG:\n"]
        grp = None
        for c in channels:
            g = c.get("channel_group__name") or "No Group"
            if g != grp:
                lines.append("\n[" + g + "]")
                grp = g
            lines.append("  id=" + str(c["id"]) + "  " + (c["name"] or ""))
        return "\n".join(lines)

    def _scan(self, cfg, log):
        import os
        from datetime import datetime
        results = self._run_matching(cfg, log)
        matched = sum(1 for r in results if r["suggestions"])
        lines   = ["EPG Suggester v2.2.0 - Scan Results",
                   str(len(results)) + " unmatched  |  suggestions found: " + str(matched), ""]
        for r in results:
            lines.append("---")
            lines.append("Channel: " + r["channel_name"] + "  [" + r["channel_group"] + "]")
            lines.append("  norm: " + r["channel_norm"])
            if r["suggestions"]:
                for i, s in enumerate(r["suggestions"], 1):
                    lines.append("  [" + str(i) + "] score=" + str(s["score"])
                                 + "  " + s["name"] + "  source=" + s["source"]
                                 + "  id=" + str(s["id"]))
            else:
                lines.append("  No suggestions above score " + str(cfg["min_s"]))
        os.makedirs("/data/exports", exist_ok=True)
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = "/data/exports/epg_suggester_scan_" + ts + ".txt"
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        log.info("EPG Suggester: scan saved to %s", out)
        preview = "\n".join(lines[:60])
        if len(lines) > 60:
            preview += "\n\n... full results in " + out
        return preview

    def _export(self, cfg, log):
        import csv, os
        from datetime import datetime
        results = self._run_matching(cfg, log)
        matched = sum(1 for r in results if r["suggestions"])
        os.makedirs("/data/exports", exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = "/data/exports/epg_suggester_" + ts + ".csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write("# EPG Suggester v2.2.0 | " + datetime.now().isoformat() + "\n")
            fh.write("# min_score=" + str(cfg["min_s"]) + "  max_suggestions=" + str(cfg["max_n"]) + "\n#\n")
            w = csv.writer(fh)
            w.writerow(["channel_id","channel_name","channel_norm","channel_group",
                        "rank","score","epg_name","tvg_id","epg_source","epg_data_id"])
            for r in results:
                if r["suggestions"]:
                    for rank, s in enumerate(r["suggestions"], 1):
                        w.writerow([r["channel_id"], r["channel_name"], r["channel_norm"],
                                    r["channel_group"], rank, s["score"], s["name"],
                                    s["tvg_id"], s["source"], s["id"]])
                else:
                    w.writerow([r["channel_id"], r["channel_name"], r["channel_norm"],
                                r["channel_group"], "", "", "NO_MATCH", "", "", ""])
        log.info("EPG Suggester: CSV saved to %s  (%d/%d matched)", path, matched, len(results))
        return ("CSV saved to " + path + "\nMatched: " + str(matched) + " / " + str(len(results))
                + "\n\ndocker cp dispatcharr:" + path + " ./")

    def _apply(self, cfg, log):
        from apps.channels.models import Channel
        if not cfg["auto"]:
            return "Auto-Apply is DISABLED. Enable it in settings first."
        results = self._run_matching(cfg, log)
        applied = skipped = failed = 0
        for r in results:
            if not r["suggestions"] or r["suggestions"][0]["score"] < cfg["thresh"]:
                skipped += 1
                continue
            top = r["suggestions"][0]
            try:
                Channel.objects.filter(pk=r["channel_id"]).update(epg_data_id=top["id"])
                log.info("EPG Suggester: APPLY  %s -> %s (score=%d)",
                         r["channel_name"], top["name"], top["score"])
                applied += 1
            except Exception as e:
                log.error("EPG Suggester: FAIL  %s -> %s", r["channel_name"], e)
                failed += 1
        return "Applied: " + str(applied) + "  Skipped: " + str(skipped) + "  Failed: " + str(failed)
