import os, json, re, html, math
from datetime import datetime, timedelta, timezone
import requests

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
ODDS_KEY = os.environ["ODDS_API_KEY"]
GEM_KEY = os.environ["GEMINI_API_KEY"]
PRIMARY_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
FALLBACK_MODEL = "gemini-3.5-flash-lite"

STATE_FILE = "state.json"
STATE_VERSION = 1
BDT = timezone(timedelta(hours=6))
ET = timezone(timedelta(hours=-5))  # NBA Schedule Timezone
NOW = datetime.now(timezone.utc)
esc = html.escape


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        if s.get("v") == STATE_VERSION:
            return s
    except Exception:
        pass
    return {
        "v": STATE_VERSION,
        "sent": {},
        "paper_picks": {},
        "stats": {"w": 0, "l": 0, "p": 0, "units": 0.0},
        "odds_cache": [],
        "odds_ts": None,
        "ratings_cache": {},
        "ratings_ts": None,
        "radar_date": None,
        "scorecard_date": None,
        "arb_sent": {}
    }


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def tg(text, use_html=True):
    try:
        data = {"chat_id": TG_CHAT, "text": text[:4000]}
        if use_html:
            data["parse_mode"] = "HTML"
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=data, timeout=20)
        if not r.ok and use_html:
            return tg(re.sub(r"<[^>]+>", "", html.unescape(text)), use_html=False)
        return r.ok
    except Exception:
        return False


def parse_dt(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def team_match(a, b):
    x, y = (a or "").lower().strip(), (b or "").lower().strip()
    if x == y or x in y or y in x:
        return True
    wx = set(x.split()) - {"los", "angeles", "new", "york", "golden", "state", "san", "antonio", "oklahoma", "city"}
    wy = set(y.split()) - {"los", "angeles", "new", "york", "golden", "state", "san", "antonio", "oklahoma", "city"}
    return bool(wx and wy and (wx & wy))


# ==================== ESPN PUBLIC API (100% FREE, NO KEY) ====================
def espn_scoreboard(dt_str):
    try:
        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            params={"dates": dt_str}, timeout=20
        )
        if r.ok:
            return r.json().get("events") or []
    except Exception:
        pass
    return []


def fetch_team_ratings(state, force=False):
    ts = state.get("ratings_ts")
    if not force and ts and NOW - parse_dt(ts) < timedelta(hours=12) and state.get("ratings_cache"):
        return state["ratings_cache"]
    ratings = {}
    try:
        r = requests.get("https://site.api.espn.com/apis/v2/sports/basketball/nba/standings", timeout=20)
        if r.ok:
            for conf in r.json().get("children") or []:
                for entry in (conf.get("standings") or {}).get("entries") or []:
                    tname = (entry.get("team") or {}).get("displayName", "")
                    smap = {st.get("name"): st.get("value") for st in (entry.get("stats") or [])}
                    pf = float(smap.get("avgPointsFor") or 114.0)
                    pa = float(smap.get("avgPointsAgainst") or 114.0)
                    w = int(smap.get("wins") or 0)
                    l = int(smap.get("losses") or 0)
                    if tname:
                        ratings[tname] = {"pf": round(pf, 2), "pa": round(pa, 2), "w": w, "l": l}
    except Exception:
        pass
    if ratings:
        state["ratings_cache"] = ratings
        state["ratings_ts"] = NOW.isoformat()
    return state.get("ratings_cache", {})


def get_rest_profile(today_et):
    played_by_day = {}
    for d_back in (1, 2, 3):
        ds = (today_et - timedelta(days=d_back)).strftime("%Y%m%d")
        teams = set()
        for ev in espn_scoreboard(ds):
            for c in (ev.get("competitions") or [{}])[0].get("competitors") or []:
                tn = (c.get("team") or {}).get("displayName")
                if tn:
                    teams.add(tn)
        played_by_day[d_back] = teams
    return played_by_day


def team_rest_status(team_name, played_by_day, is_home):
    b2b = team_name in played_by_day.get(1, set())
    cnt_3d = sum(1 for d in (1, 2, 3) if team_name in played_by_day.get(d, set()))
    if b2b:
        pen = -1.4 if is_home else -2.3
        tag = "Home B2B" if is_home else "Away B2B"
        if cnt_3d >= 2:
            pen -= 0.7
            tag += " (3-in-4)"
        return {"days": 0, "pen": pen, "tag": tag, "b2b": True}
    if team_name in played_by_day.get(2, set()):
        return {"days": 1, "pen": 0.0, "tag": "1 Day Rest", "b2b": False}
    return {"days": 2, "pen": 0.3, "tag": "2+ Days Rest", "b2b": False}


def parse_espn_game(ev):
    comp = (ev.get("competitions") or [{}])[0]
    comps = comp.get("competitors") or []
    home = next((c for c in comps if c.get("homeAway") == "home"), None)
    away = next((c for c in comps if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    st_obj = (comp.get("status") or {})
    st_type = (st_obj.get("type") or {})
    state_name = st_type.get("name", "")
    period = int(st_obj.get("period") or 0)
    clock = st_obj.get("displayClock", "")

    def get_3pt(c):
        for s in c.get("statistics") or []:
            if s.get("name") == "threePointFieldGoalPct":
                try:
                    return float(s.get("displayValue", 0))
                except Exception:
                    return None
        return None

    return {
        "id": ev["id"],
        "start": parse_dt(ev.get("date")),
        "venue": (comp.get("venue") or {}).get("fullName", "NBA Arena"),
        "home": (home.get("team") or {}).get("displayName", ""),
        "away": (away.get("team") or {}).get("displayName", ""),
        "h_score": int(home.get("score") or 0),
        "a_score": int(away.get("score") or 0),
        "h_3pt": get_3pt(home),
        "a_3pt": get_3pt(away),
        "status": state_name,
        "status_txt": st_type.get("shortDetail", ""),
        "period": period,
        "clock": clock
    }


# ==================== ODDS API (CACHED TO SAVE CREDITS) ====================
def fetch_nba_odds(state, games, force=False):
    has_soon = any(
        g["start"] and -180 <= (g["start"] - NOW).total_seconds() / 60 <= 480
        and g["status"] != "STATUS_FINAL"
        for g in games
    )
    if not force and not has_soon:
        return state.get("odds_cache", [])
    ts = state.get("odds_ts")
    if not force and ts and NOW - parse_dt(ts) < timedelta(minutes=75) and state.get("odds_cache"):
        return state["odds_cache"]
    try:
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/basketball_nba/odds/",
            params={
                "apiKey": ODDS_KEY, "regions": "us",
                "markets": "h2h,spreads,totals", "oddsFormat": "decimal"
            }, timeout=20
        )
        if r.ok:
            data = r.json()
            state["odds_cache"] = data
            state["odds_ts"] = NOW.isoformat()
            return data
    except Exception:
        pass
    return state.get("odds_cache", [])


def extract_market(game, odds_events):
    ev = next(
        (e for e in odds_events if team_match(game["home"], e.get("home_team")) and team_match(game["away"], e.get("away_team"))),
        None
    )
    if not ev:
        return None
    h_ml, a_ml, h_nv = [], [], []
    h_spreads, h_sp_odds, a_sp_odds = [], [], []
    tot_lines, over_odds, under_odds = [], [], []
    best_h = (0.0, "")
    best_a = (0.0, "")

    for bm in ev.get("bookmakers") or []:
        btitle = bm.get("title", "Book")
        for mk in bm.get("markets") or []:
            key = mk.get("key")
            outs = mk.get("outcomes") or []
            if key == "h2h" and len(outs) >= 2:
                ho = next((o["price"] for o in outs if team_match(game["home"], o.get("name"))), None)
                ao = next((o["price"] for o in outs if team_match(game["away"], o.get("name"))), None)
                if ho and ao:
                    h_ml.append(ho)
                    a_ml.append(ao)
                    s = (1.0 / ho) + (1.0 / ao)
                    h_nv.append((1.0 / ho) / s * 100.0)
                    if ho > best_h[0]:
                        best_h = (ho, btitle)
                    if ao > best_a[0]:
                        best_a = (ao, btitle)
            elif key == "spreads" and len(outs) >= 2:
                ho = next((o for o in outs if team_match(game["home"], o.get("name"))), None)
                ao = next((o for o in outs if team_match(game["away"], o.get("name"))), None)
                if ho and ao and ho.get("point") is not None:
                    h_spreads.append(float(ho["point"]))
                    h_sp_odds.append(float(ho["price"]))
                    a_sp_odds.append(float(ao["price"]))
            elif key == "totals" and len(outs) >= 2:
                ov = next((o for o in outs if o.get("name") == "Over"), None)
                un = next((o for o in outs if o.get("name") == "Under"), None)
                if ov and un and ov.get("point") is not None:
                    tot_lines.append(float(ov["point"]))
                    over_odds.append(float(ov["price"]))
                    under_odds.append(float(un["price"]))

    if not h_ml:
        return None
    arb = None
    if best_h[0] > 1.0 and best_a[0] > 1.0:
        inv_sum = (1.0 / best_h[0]) + (1.0 / best_a[0])
        if inv_sum < 0.995:
            profit_pct = round(((1.0 / inv_sum) - 1.0) * 100.0, 2)
            stake_h = round((1.0 / best_h[0]) / inv_sum * 100.0, 1)
            arb = {
                "profit_pct": profit_pct,
                "home_odds": best_h[0], "home_book": best_h[1], "stake_h": stake_h,
                "away_odds": best_a[0], "away_book": best_a[1], "stake_a": round(100.0 - stake_h, 1)
            }
    return {
        "h_odds": round(sum(h_ml) / len(h_ml), 2),
        "a_odds": round(sum(a_ml) / len(a_ml), 2),
        "h_nv": round(sum(h_nv) / len(h_nv), 1),
        "a_nv": round(100.0 - sum(h_nv) / len(h_nv), 1),
        "h_spread": round(sum(h_spreads) / len(h_spreads) * 2) / 2 if h_spreads else None,
        "h_sp_odds": round(sum(h_sp_odds) / len(h_sp_odds), 2) if h_sp_odds else 1.91,
        "a_sp_odds": round(sum(a_sp_odds) / len(a_sp_odds), 2) if a_sp_odds else 1.91,
        "total": round(sum(tot_lines) / len(tot_lines) * 2) / 2 if tot_lines else None,
        "over_odds": round(sum(over_odds) / len(over_odds), 2) if over_odds else 1.91,
        "under_odds": round(sum(under_odds) / len(under_odds), 2) if under_odds else 1.91,
        "arb": arb
    }


# ==================== PYTHON MATH ENGINE (OLIVER + STERN) ====================
def compute_nba_math(game, ratings, rest_h, rest_a, market):
    lg_avg = (
        sum(v["pf"] for v in ratings.values()) / len(ratings)
        if ratings else 114.0
    )
    rh = ratings.get(game["home"], {"pf": lg_avg, "pa": lg_avg, "w": 0, "l": 0})
    ra = ratings.get(game["away"], {"pf": lg_avg, "pa": lg_avg, "w": 0, "l": 0})

    # Dean Oliver Pace/Efficiency Proxy + HCA (+2.2) + Rest/Fatigue
    proj_h = (rh["pf"] + ra["pa"] - lg_avg) + 1.1 + rest_h["pen"]
    proj_a = (ra["pf"] + rh["pa"] - lg_avg) - 1.1 + rest_a["pen"]
    margin = proj_h - proj_a
    proj_total = proj_h + proj_a

    # Normal Distribution Win % (sigma = 12.0)
    win_h = round(min(max(norm_cdf(margin / 12.0) * 100.0, 5.0), 95.0), 1)
    win_a = round(100.0 - win_h, 1)

    candidates = []
    if market:
        # Moneyline edge
        ml_edge_h = round(win_h - market["h_nv"], 1)
        ml_edge_a = round(win_a - market["a_nv"], 1)
        if ml_edge_h >= ml_edge_a:
            candidates.append({
                "type": "ML", "side": "home", "team": game["home"],
                "label": f"{game['home']} ML", "line": 0.0,
                "odds": market["h_odds"], "model_p": win_h, "mkt_p": market["h_nv"], "edge": ml_edge_h
            })
        else:
            candidates.append({
                "type": "ML", "side": "away", "team": game["away"],
                "label": f"{game['away']} ML", "line": 0.0,
                "odds": market["a_odds"], "model_p": win_a, "mkt_p": market["a_nv"], "edge": ml_edge_a
            })

        # Spread edge
        if market["h_spread"] is not None:
            sp_h = market["h_spread"]
            cov_h = round(norm_cdf((margin + sp_h) / 12.0) * 100.0, 1)
            cov_a = round(100.0 - cov_h, 1)
            if cov_h >= cov_a:
                candidates.append({
                    "type": "SPREAD", "side": "home", "team": game["home"],
                    "label": f"{game['home']} {sp_h:+.1f}", "line": sp_h,
                    "odds": market["h_sp_odds"], "model_p": cov_h, "mkt_p": 50.0, "edge": round(cov_h - 50.0, 1)
                })
            else:
                candidates.append({
                    "type": "SPREAD", "side": "away", "team": game["away"],
                    "label": f"{game['away']} {-sp_h:+.1f}", "line": -sp_h,
                    "odds": market["a_sp_odds"], "model_p": cov_a, "mkt_p": 50.0, "edge": round(cov_a - 50.0, 1)
                })

        # Totals edge (sigma = 15.5)
        if market["total"] is not None:
            t_line = market["total"]
            over_p = round(norm_cdf((proj_total - t_line) / 15.5) * 100.0, 1)
            under_p = round(100.0 - over_p, 1)
            if over_p >= under_p:
                candidates.append({
                    "type": "TOTAL", "side": "over", "team": "Over",
                    "label": f"Game Over {t_line:.1f}", "line": t_line,
                    "odds": market["over_odds"], "model_p": over_p, "mkt_p": 50.0, "edge": round(over_p - 50.0, 1)
                })
            else:
                candidates.append({
                    "type": "TOTAL", "side": "under", "team": "Under",
                    "label": f"Game Under {t_line:.1f}", "line": t_line,
                    "odds": market["under_odds"], "model_p": under_p, "mkt_p": 50.0, "edge": round(under_p - 50.0, 1)
                })

    best = max(candidates, key=lambda c: c["edge"]) if candidates else None
    rest_gap = rest_h["days"] - rest_a["days"]
    fatigue_mismatch = (rest_h["b2b"] != rest_a["b2b"]) and abs(rest_gap) >= 1
    is_sniper = bool(
        best and (
            best["edge"] >= 6.5
            or (fatigue_mismatch and best["edge"] >= 4.5)
        )
    )

    # Hal Stern Live Brownian Motion (if live)
    live_h_p, live_a_p, shoot_flag = None, None, "Normal"
    if game["status"] in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME", "STATUS_END_PERIOD"):
        if game["status"] == "STATUS_HALFTIME" or (game["period"] == 2 and game["status"] == "STATUS_END_PERIOD"):
            t_rem = 0.50
        elif game["period"] == 3 and game["status"] == "STATUS_END_PERIOD":
            t_rem = 0.25
        else:
            t_rem = max(0.08, (4 - min(game["period"], 4) + 0.5) / 4.0)
        lead = game["h_score"] - game["a_score"]
        live_h_p = round(min(max(norm_cdf((lead + t_rem * margin) / (12.0 * math.sqrt(t_rem))) * 100.0, 2.0), 98.0), 1)
        live_a_p = round(100.0 - live_h_p, 1)
        flags = []
        if game["h_3pt"] is not None and game["h_3pt"] >= 48.0:
            flags.append(f"{game['home']} unsustainably hot 3PT ({game['h_3pt']:.0f}% — Regression Risk)")
        if game["a_3pt"] is not None and game["a_3pt"] >= 48.0:
            flags.append(f"{game['away']} unsustainably hot 3PT ({game['a_3pt']:.0f}% — Regression Risk)")
        if flags:
            shoot_flag = " | ".join(flags)

    return {
        "proj_h": round(proj_h, 1), "proj_a": round(proj_a, 1),
        "margin": round(margin, 1), "proj_total": round(proj_total, 1),
        "win_h": win_h, "win_a": win_a,
        "best": best, "is_sniper": is_sniper, "fatigue_mismatch": fatigue_mismatch,
        "live_h_p": live_h_p, "live_a_p": live_a_p, "shoot_flag": shoot_flag
    }


# ==================== GEMINI INJURY AUDIT & REPORT ====================
def call_gemini(prompt):
    for model in (PRIMARY_MODEL, FALLBACK_MODEL):
        for use_search in (True, False):
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 1200, "temperature": 0.2}
            }
            if use_search:
                payload["tools"] = [{"google_search": {}}]
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": GEM_KEY}, json=payload, timeout=75
                )
                if r.ok:
                    txt = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"]).strip()
                    if txt:
                        return txt
            except Exception:
                pass
    return None


def to_html(t):
    lines = []
    for raw in t.splitlines():
        s = raw.strip()
        q = s.startswith(">")
        c = s.lstrip(">").strip() if q else raw
        line = esc(c)
        line = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", line)
        line = line.replace("`", "").replace("**", "")
        lines.append(f"<blockquote>{line}</blockquote>" if q else line)
    return "\n".join(lines)


def send_sniper_card(game, stage_badge, rest_h, rest_a, market, mp, is_paper=False):
    bdt_time = game["start"].astimezone(BDT).strftime("%I:%M %p BDT")
    best = mp["best"]
    pick_txt = f"{best['label']} @ {best['odds']} (Model {best['model_p']}% vs Mkt {best['mkt_p']}% | Edge {best['edge']:+.1f}%)" if best else "No Market Open Yet"
    mkt_line = (
        f"ML: {game['home']} {market['h_odds']} ({market['h_nv']}%) vs {game['away']} {market['a_odds']} ({market['a_nv']}%) | "
        f"Spread: {game['home']} {market['h_spread']:+.1f} | Total: {market['total']}"
        if market and market["h_spread"] is not None else "Odds Pending"
    )
    live_info = (
        f"Live Score: {game['home']} {game['h_score']} - {game['away']} {game['a_score']} ({game['status_txt']}) | "
        f"Stern Live Win%: {game['home']} {mp['live_h_p']}% vs {game['away']} {mp['live_a_p']}% | 3PT Audit: {mp['shoot_flag']}"
        if mp["live_h_p"] is not None else "Pre-Tipoff"
    )

    prompt = f"""You are a quantitative NBA betting analyst. Write a concise, sharp 3-block report in English.
DATA:
- Match: {game['home']} (Home) vs {game['away']} (Away) | Venue: {game['venue']}
- Rest/Fatigue: {game['home']} = {rest_h['tag']} | {game['away']} = {rest_a['tag']}
- Market Lines: {mkt_line}
- Python Locked Projections: {game['home']} {mp['proj_h']} – {game['away']} {mp['proj_a']} (Margin {mp['margin']:+.1f} | Proj Total {mp['proj_total']})
- Python Locked Win%: {game['home']} {mp['win_h']}% vs {game['away']} {mp['win_a']}%
- Top Model Edge: {pick_txt}
- Live State: {live_info}

STRICT RULES:
- Use Google Search to check TODAY'S latest NBA Injury Report for {game['home']} and {game['away']} (list any star player OUT or Questionable; if injury destroys the edge, explicitly warn `⚠️ INJURY CAUTION — SKIP`).
- NEVER change any Python-calculated numbers above. Keep it crisp and scannable.
- Exact 3-block format:

━━━ 🩺 **FATIGUE & INJURY AUDIT** ━━━
1. **Rest & Schedule Edge:** 🔵 **`{game['home']}`** ({rest_h['tag']}) vs 🟠 **`{game['away']}`** ({rest_a['tag']})
2. **Latest Injury Report:** (confirmed OUT/Questionable star players from today's NBA injury news)
3. **Tempo & Tactical Note:** (1-2 crisp lines on matchup & live 3PT/fatigue angle if live)

━━━ 📈 **MATH ENGINE VS MARKET (LOCKED)** ━━━
• **Projected Score:** 🔵 **`{game['home']}`** `{mp['proj_h']}` – 🟠 **`{game['away']}`** `{mp['proj_a']}` (Total `{mp['proj_total']}`)
• **Market Lines:** {mkt_line}
• **Best Math Edge:** **`{pick_txt}`**

> 🎯 **Sniper Verdict:** **`{best['label'] if best else 'MONITOR'}`** — {'A+ SNIPER (AUTO-LOGGED 1u PAPER)' if is_paper else ('CLEAR VALUE EDGE' if mp['is_sniper'] else 'WATCHLIST / SKIP')}"""

    body = call_gemini(prompt)
    head = (
        f"🏀 <b>NBA {esc(stage_badge)}</b>\n"
        f"🔵 <b><code>{esc(game['home'])}</code></b> vs 🟠 <b><code>{esc(game['away'])}</code></b>\n"
        f"🏟️ {esc(game['venue'])} | ⏰ <code>{bdt_time}</code>\n\n"
    )
    fallback = (
        f"<b>Rest:</b> {esc(rest_h['tag'])} vs {esc(rest_a['tag'])}\n"
        f"<b>Proj:</b> {mp['proj_h']} – {mp['proj_a']} (Win {mp['win_h']}% / {mp['win_a']}%)\n"
        f"<b>Edge:</b> <code>{esc(pick_txt)}</code>"
    )
    return tg(head + (to_html(body[:3400]) if body else fallback))


# ==================== AUTO PAPER-TRADING SETTLEMENT ====================
def settle_paper_picks(state, all_games):
    gmap = {g["id"]: g for g in all_games}
    settled_lines = []
    for gid, pk in list(state["paper_picks"].items()):
        if pk.get("status") != "pending":
            continue
        g = gmap.get(gid)
        if not g or g["status"] != "STATUS_FINAL":
            continue
        hs, as_ = g["h_score"], g["a_score"]
        margin = hs - as_
        total = hs + as_
        res = "LOSS"
        if pk["type"] == "ML":
            won = (margin > 0) if pk["side"] == "home" else (margin < 0)
            res = "WIN" if won else "LOSS"
        elif pk["type"] == "SPREAD":
            val = (margin + pk["line"]) if pk["side"] == "home" else (-margin + pk["line"])
            res = "WIN" if val > 0 else ("PUSH" if val == 0 else "LOSS")
        elif pk["type"] == "TOTAL":
            diff = (total - pk["line"]) if pk["side"] == "over" else (pk["line"] - total)
            res = "WIN" if diff > 0 else ("PUSH" if diff == 0 else "LOSS")

        odds = float(pk.get("odds") or 1.91)
        pnl = round(odds - 1.0, 2) if res == "WIN" else (0.0 if res == "PUSH" else -1.0)
        pk["status"] = res
        pk["pnl"] = pnl
        pk["final"] = f"{g['home']} {hs}–{as_} {g['away']}"
        st = state["stats"]
        if res == "WIN":
            st["w"] += 1
        elif res == "LOSS":
            st["l"] += 1
        else:
            st["p"] += 1
        st["units"] = round(st["units"] + pnl, 2)
        icon = "✅" if res == "WIN" else ("➖" if res == "PUSH" else "❌")
        settled_lines.append(f"{icon} <b>{esc(pk['label'])}</b> @ {odds} ➔ <code>{res} ({pnl:+.2f}u)</code>\n   Final: {esc(pk['final'])}")

    if settled_lines:
        st = state["stats"]
        tot_decided = st["w"] + st["l"]
        wr = round((st["w"] / tot_decided) * 100.0, 1) if tot_decided else 0.0
        roi = round((st["units"] / max(tot_decided, 1)) * 100.0, 1)
        msg = (
            f"📊 <b>NBA AUTO PAPER-TRADING SCORECARD</b>\n\n"
            + "\n".join(settled_lines)
            + f"\n\n<blockquote>🏆 <b>All-Time Virtual Record:</b> <code>{st['w']}W - {st['l']}L - {st['p']}P</code> "
              f"| Win Rate: <b><code>{wr}%</code></b> | Net P/L: <b><code>{st['units']:+.2f}u</code></b> (ROI: <code>{roi:+.1f}%</code>)</blockquote>"
        )
        tg(msg)


def main():
    state = load_state()
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    now_et = NOW.astimezone(ET)
    today_et_str = now_et.strftime("%Y%m%d")
    yest_et_str = (now_et - timedelta(days=1)).strftime("%Y%m%d")

    ratings = fetch_team_ratings(state, force=is_manual)
    rest_map = get_rest_profile(now_et)
    today_games = [g for g in (parse_espn_game(e) for e in espn_scoreboard(today_et_str)) if g]
    yest_games = [g for g in (parse_espn_game(e) for e in espn_scoreboard(yest_et_str)) if g]

    # 1. Auto-settle any finished Paper-Trading picks
    settle_paper_picks(state, today_games + yest_games)

    # 2. Fetch cached Odds
    odds_events = fetch_nba_odds(state, today_games, force=is_manual)

    # 3. Daily Fatigue Radar (once per NBA slate day)
    bdt_today = NOW.astimezone(BDT).date().isoformat()
    if today_games and (state.get("radar_date") != today_et_str or is_manual):
        radar_lines = []
        for i, g in enumerate(today_games, 1):
            rh = team_rest_status(g["home"], rest_map, True)
            ra = team_rest_status(g["away"], rest_map, False)
            flag = "🔥 FATIGUE EDGE" if (rh["b2b"] != ra["b2b"]) else "⚖️ Even Rest"
            tm = g["start"].astimezone(BDT).strftime("%I:%M %p")
            radar_lines.append(
                f"{i}. {flag} | ⏰ <code>{tm} BDT</code>\n"
                f"   🔵 <b>{esc(g['home'])}</b> ({rh['tag']}) vs 🟠 <b>{esc(g['away'])}</b> ({ra['tag']})"
            )
        st = state["stats"]
        head = (
            f"🏀 <b>NBA DAILY FATIGUE RADAR (<code>{bdt_today}</code>)</b>\n"
            f"📊 <code>Games: {len(today_games)} | Paper Record: {st['w']}W-{st['l']}L ({st['units']:+.2f}u)</code>\n\n"
        )
        if tg(head + "\n".join(radar_lines)):
            state["radar_date"] = today_et_str

    # 4. Scan Games: Arbitrage + Silent A+ Sniper + Live Hedge/Halftime/Q3
    cards_sent = 0
    best_preview = None
    for g in today_games:
        gid = g["id"]
        if g["status"] == "STATUS_FINAL" or not g["start"]:
            continue
        mins = (g["start"] - NOW).total_seconds() / 60
        sent = state["sent"].setdefault(gid, [])
        rh = team_rest_status(g["home"], rest_map, True)
        ra = team_rest_status(g["away"], rest_map, False)
        mkt = extract_market(g, odds_events)
        mp = compute_nba_math(g, ratings, rh, ra, mkt)

        # Track best game for manual preview
        if mp["best"] and (not best_preview or mp["best"]["edge"] > best_preview[0]):
            best_preview = (mp["best"]["edge"], g, rh, ra, mkt, mp)

        # A. Cross-Book Arbitrage Scanner (0% Math Risk)
        if mkt and mkt.get("arb") and gid not in state["arb_sent"]:
            arb = mkt["arb"]
            arb_msg = (
                f"💎 <b>NBA ARBITRAGE FOUND (+{arb['profit_pct']}% RISK-FREE)</b>\n"
                f"🔵 <b><code>{esc(g['home'])}</code></b> vs 🟠 <b><code>{esc(g['away'])}</code></b>\n\n"
                f"• Bet <b>{arb['stake_h']}%</b> on <b>{esc(g['home'])}</b> @ <code>{arb['home_odds']}</code> ({esc(arb['home_book'])})\n"
                f"• Bet <b>{arb['stake_a']}%</b> on <b>{esc(g['away'])}</b> @ <code>{arb['away_odds']}</code> ({esc(arb['away_book'])})\n"
                f"<blockquote>Guaranteed profit regardless of winner if placed simultaneously.</blockquote>"
            )
            if tg(arb_msg):
                state["arb_sent"][gid] = NOW.isoformat()

        # B. Silent A+ Sniper Window (20-55m Pre-Tipoff)
        if 15 <= mins <= 55 and "sniper" not in sent and mp["is_sniper"]:
            if send_sniper_card(g, "🎯 A+ SNIPER ALERT", rh, ra, mkt, mp, is_paper=True):
                sent.append("sniper")
                pk = mp["best"]
                state["paper_picks"][gid] = {
                    "type": pk["type"], "side": pk["side"], "team": pk["team"],
                    "label": pk["label"], "line": pk["line"], "odds": pk["odds"],
                    "status": "pending", "date": today_et_str
                }
                cards_sent += 1

        # C. Live Halftime / End of Q3 Hedge & Cash-Out Window (ONLY for tracked Sniper games!)
        if gid in state["paper_picks"] and state["paper_picks"][gid]["status"] == "pending":
            pk = state["paper_picks"][gid]
            is_ht = g["status"] == "STATUS_HALFTIME" or (g["period"] == 2 and g["status"] == "STATUS_END_PERIOD")
            is_q3 = g["period"] == 3 and g["status"] == "STATUS_END_PERIOD"
            stage_tag = "ht" if is_ht else ("q3" if is_q3 else None)
            if stage_tag and stage_tag not in sent and mp["live_h_p"] is not None:
                lead = g["h_score"] - g["a_score"]
                backed_lead = lead if pk["side"] == "home" else (-lead if pk["side"] == "away" else 0)
                backed_live_p = mp["live_h_p"] if pk["side"] == "home" else mp["live_a_p"]
                hedge_note = (
                    "🔒 LOCK PROFIT / HEDGE WINDOW"
                    if (pk["type"] in ("ML", "SPREAD") and (backed_lead >= 12 or backed_live_p >= 85.0))
                    else ("Halftime Pulse" if is_ht else "End of Q3 Pulse")
                )
                if send_sniper_card(g, hedge_note, rh, ra, mkt, mp, is_paper=False):
                    sent.append(stage_tag)
                    cards_sent += 1

    # Manual test preview card (so you see the format immediately)
    if is_manual and cards_sent == 0:
        if best_preview:
            _, g, rh, ra, mkt, mp = best_preview
            send_sniper_card(g, "PREVIEW SNAPSHOT (TOP EDGE TODAY)", rh, ra, mkt, mp, is_paper=False)
        elif today_games:
            g = today_games[0]
            rh = team_rest_status(g["home"], rest_map, True)
            ra = team_rest_status(g["away"], rest_map, False)
            mkt = extract_market(g, odds_events)
            mp = compute_nba_math(g, ratings, rh, ra, mkt)
            send_sniper_card(g, "PREVIEW SNAPSHOT", rh, ra, mkt, mp, is_paper=False)

    # Cleanup old non-pending keys
    keep_ids = {g["id"] for g in today_games} | {k for k, v in state["paper_picks"].items() if v.get("status") == "pending"}
    state["sent"] = {k: v for k, v in state["sent"].items() if k in keep_ids}
    state["arb_sent"] = {k: v for k, v in state["arb_sent"].items() if k in keep_ids}
    save_state(state)


if __name__ == "__main__":
    main()
