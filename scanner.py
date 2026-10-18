#!/usr/bin/env python3
"""RepoRadar – napi GitHub-repó szkenner.

Források:
  * GitHub keresés  – friss repók, amik gyorsan gyűjtik a csillagokat
  * GitHub Trending – mi pörög ma
  * Hacker News     – amiről a legtöbben beszélnek
A Gemini magyar összefoglalót ír, és a korábbi döntéseid alapján
pontozza, mennyire illik hozzád egy repó.
"""
import datetime as dt
import json
import math
import os
import re
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CFG = json.loads((ROOT / "config.json").read_text("utf-8"))

TODAY_D = dt.datetime.now(dt.timezone.utc).date()
TODAY = TODAY_D.isoformat()

GH_API = "https://api.github.com"
GH_TOKEN = os.getenv("GITHUB_TOKEN", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or CFG.get("gemini_model", "gemini-flash-latest")

S = requests.Session()
S.headers["User-Agent"] = "RepoRadar/1.0"


# ---------------------------------------------------------------- segédek
def days_ago(n):
    return (TODAY_D - dt.timedelta(days=n)).isoformat()


def load(name, default):
    try:
        return json.loads((DATA / name).read_text("utf-8"))
    except Exception:
        return default


def save(name, obj):
    DATA.mkdir(exist_ok=True)
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")


def gh(path, params=None, raw=False):
    headers = {
        "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"
    for attempt in range(3):
        try:
            r = S.get(GH_API + path, headers=headers, params=params, timeout=30)
        except requests.RequestException as e:
            print("  hálózati hiba:", e)
            time.sleep(5)
            continue
        if r.status_code in (403, 429) and "rate limit" in r.text.lower() and attempt < 2:
            print("  GitHub limit, várok 30 mp-et…")
            time.sleep(30)
            continue
        return r
    return None


# ---------------------------------------------------------------- források
def from_search():
    """Friss és témához tartozó repók a GitHub keresőből."""
    since = days_ago(CFG.get("new_repo_days", 7))
    queries = [f"created:>{since} stars:>{CFG.get('min_stars_new', 40)}"]
    for topic in CFG.get("topics", []):
        queries.append(f"topic:{topic} pushed:>{since} stars:>{CFG.get('min_stars_topic', 150)}")
    out = {}
    for q in queries:
        r = gh("/search/repositories", {"q": q, "sort": "stars", "order": "desc", "per_page": 40})
        if r is not None and r.ok:
            for it in r.json().get("items", []):
                out[it["full_name"]] = it
        else:
            print("  keresési hiba:", q, r.status_code if r is not None else "-")
        time.sleep(2)
    print(f"Keresés: {len(out)} repó")
    return out


def from_trending():
    """GitHub Trending oldal (nincs rá hivatalos API, ezért HTML-ből olvassuk)."""
    out = {}
    for lang in CFG.get("trending_languages", [""]):
        url = "https://github.com/trending" + (f"/{lang}" if lang else "") + "?since=daily"
        try:
            html = S.get(url, timeout=30).text
        except requests.RequestException as e:
            print("  trending hiba:", e)
            continue
        for block in html.split('<article class="Box-row">')[1:]:
            m = re.search(r'<h2[^>]*>\s*<a[^>]*href="/([^/"\s]+/[^/"\s]+)"', block)
            if not m:
                continue
            n = re.search(r"([\d,]+)\s+stars?\s+today", block)
            today = int(n.group(1).replace(",", "")) if n else 0
            out[m.group(1)] = max(out.get(m.group(1), 0), today)
        time.sleep(1)
    print(f"Trending: {len(out)} repó")
    return out


def from_hn():
    """Hacker News sztorik az elmúlt 2 napból, amik GitHub-repóra mutatnak."""
    since = int(time.time()) - 2 * 86400
    out = {}
    try:
        r = S.get(
            "https://hn.algolia.com/api/v1/search",
            params={
                "query": "github.com",
                "tags": "story",
                "numericFilters": f"created_at_i>{since},points>{CFG.get('hn_min_points', 25)}",
                "hitsPerPage": 100,
            },
            timeout=30,
        )
        hits = r.json().get("hits", [])
    except Exception as e:
        print("  HN hiba:", e)
        return out
    for h in hits:
        m = re.match(r"https?://(?:www\.)?github\.com/([\w.-]+/[\w.-]+)", h.get("url") or "")
        if not m:
            continue
        full = m.group(1).removesuffix(".git")
        cur = out.setdefault(full, {"points": 0, "comments": 0, "url": ""})
        if (h.get("points") or 0) > cur["points"]:
            cur.update(
                points=h.get("points") or 0,
                comments=h.get("num_comments") or 0,
                url=f"https://news.ycombinator.com/item?id={h['objectID']}",
            )
    print(f"Hacker News: {len(out)} repó")
    return out


# ---------------------------------------------------------------- részletek
def repo_details(full):
    r = gh(f"/repos/{full}")
    return r.json() if r is not None and r.ok else None


def downloads(full):
    r = gh(f"/repos/{full}/releases", {"per_page": 10})
    if r is None or not r.ok:
        return 0
    return sum(a.get("download_count", 0) for rel in r.json() for a in rel.get("assets", []))


def readme(full, limit=2500):
    r = gh(f"/repos/{full}/readme", raw=True)
    if r is None or not r.ok:
        return ""
    t = r.text
    t = re.sub(r"<[^>]+>", " ", t)                 # HTML-címkék
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)       # képek
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)   # linkek -> szöveg
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()[:limit]


def growth_per_day(entry, stars, created):
    """Napi csillagnövekedés: korábbi mérésből, vagy új repónál az életkorából."""
    prev = [(d, n) for d, n in entry.get("stars", {}).items() if d < TODAY]
    if prev:
        d, n = max(prev)
        days = max(1, (TODAY_D - dt.date.fromisoformat(d)).days)
        return max(0.0, (stars - n) / days)
    if created:
        age = max(1, (TODAY_D - dt.date.fromisoformat(created[:10])).days)
        if age <= 30:
            return stars / age
    return 0.0


# ---------------------------------------------------------------- Gemini
def build_prompt(batch, liked, disliked):
    lines = [
        "Te egy GitHub-repó felderítő vagy. A felhasználóról:",
        CFG.get("about", ""),
        "",
        "Ezeket korábban érdekesnek jelölte:",
        "\n".join(liked) or "(még nincs)",
        "",
        "Ezek nem érdekelték:",
        "\n".join(disliked) or "(még nincs)",
        "",
        "Minden alábbi repóhoz adj vissza egy objektumot egy JSON-tömbben, ezekkel a mezőkkel:",
        '"repo": a pontos név (tulajdonos/repó),',
        '"cim": rövid magyar cím, ami elmondja, mi ez (legfeljebb 6 szó),',
        '"mit_csinal": 2-3 egyszerű magyar mondat, szakzsargon nélkül,',
        '"mire_jo": 1 mondat arról, mire tudná ő konkrétan használni vagy beépíteni,',
        '"kategoria": egy szó (AI, Android, eszköz, web, játék, adat, biztonság, oktatás, egyéb),',
        '"telefonrol": "igen", "részben" vagy "nem" – kipróbálható-e telefonról is (PC nélkül, pl. böngészőben vagy GitHub Actionsből),',
        '"egyezes": 0-100 egész szám, mennyire illik az érdeklődéséhez,',
        '"miert": legfeljebb 1 mondat, miért ennyi az egyezés.',
        "Csak a JSON-tömböt add vissza, semmi mást.",
        "",
        "Repók:",
    ]
    for it in batch:
        lines += [
            f"### {it['full']}",
            f"Leírás: {it['desc']}",
            f"Nyelv: {it['lang']} | Csillag: {it['stars']} | Témák: {', '.join(it['topics'])}",
            "README eleje:",
            it.get("_readme", "") or "(nincs)",
            "",
        ]
    return "\n".join(lines)


def gemini_summaries(items, liked, disliked):
    if not GEMINI_KEY:
        print("Nincs GEMINI_API_KEY – összefoglaló nélkül megyünk tovább.")
        return {}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    out = {}
    for i in range(0, len(items), 5):
        batch = items[i : i + 5]
        body = {
            "contents": [{"parts": [{"text": build_prompt(batch, liked, disliked)}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
        }
        for attempt in range(3):
            try:
                r = S.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=180)
                if r.status_code in (429, 500, 503):
                    print(f"  Gemini {r.status_code}, várok…")
                    time.sleep(40)
                    continue
                r.raise_for_status()
                parts = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                arr = json.loads(text)
                if isinstance(arr, dict):  # néha egy kulcs alá csomagolja
                    arr = next((v for v in arr.values() if isinstance(v, list)), [arr])
                for o in arr:
                    if isinstance(o, dict) and o.get("repo"):
                        out[o["repo"].lower()] = o
                break
            except Exception as e:
                print("  Gemini hiba:", e)
                time.sleep(10)
        time.sleep(7)  # ingyenes keret: kíméljük a percenkénti limitet
    print(f"Gemini: {len(out)} összefoglaló ({GEMINI_MODEL})")
    return out


# ---------------------------------------------------------------- fő folyamat
def main():
    history = load("history.json", {})
    feedback = load("feedback.json", {})
    previous = load("repos.json", {})
    rated = {k.lower() for k in feedback}

    cands = {}

    def add(full, **kw):
        c = cands.setdefault(full.lower(), {"full": full, "repo": None, "trend": 0, "hn": None})
        c.update(kw)

    for full, it in from_search().items():
        add(full, repo=it)
    for full, n in from_trending().items():
        add(full, trend=n)
    for full, h in from_hn().items():
        add(full, hn=h)

    shown_cutoff = days_ago(CFG.get("reshow_after_days", 30))
    pool = [
        c for k, c in cands.items()
        if k not in rated and history.get(k, {}).get("shown", "") <= shown_cutoff
    ][:180]

    for c in pool:
        if c["repo"] is None:
            c["repo"] = repo_details(c["full"])

    items = []
    for c in pool:
        repo = c["repo"]
        if not repo or repo.get("fork") or repo.get("archived"):
            continue
        k = repo["full_name"].lower()
        entry = history.setdefault(k, {"stars": {}})
        stars = repo["stargazers_count"]
        g = max(float(c["trend"]), growth_per_day(entry, stars, repo.get("created_at")))
        entry["stars"][TODAY] = stars
        entry["seen"] = TODAY
        hn = c["hn"] or {}
        buzz = (
            math.log1p(g) * 1.0
            + math.log1p(hn.get("points", 0)) * 1.2
            + math.log1p(hn.get("comments", 0)) * 0.4
            + math.log1p(stars) * 0.2
        )
        src = []
        if (repo.get("created_at") or "")[:10] >= days_ago(30):
            src.append("új")
        if c["trend"] > 0:
            src.append("trending")
        if hn:
            src.append("hn")
        items.append({
            "full": repo["full_name"],
            "url": repo["html_url"],
            "desc": repo.get("description") or "",
            "lang": repo.get("language") or "",
            "stars": stars,
            "forks": repo.get("forks_count", 0),
            "growth": round(g, 1),
            "hn_points": hn.get("points", 0),
            "hn_comments": hn.get("comments", 0),
            "hn_url": hn.get("url", ""),
            "topics": (repo.get("topics") or [])[:6],
            "created": (repo.get("created_at") or "")[:10],
            "pushed": (repo.get("pushed_at") or "")[:10],
            "license": ((repo.get("license") or {}).get("spdx_id") or "").replace("NOASSERTION", ""),
            "homepage": repo.get("homepage") or "",
            "src": src,
            "buzz": round(buzz, 3),
            "first_seen": TODAY,
        })

    items.sort(key=lambda x: x["buzz"], reverse=True)
    items = items[: CFG.get("max_items", 25)]

    for it in items:
        it["_readme"] = readme(it["full"])
        it["downloads"] = downloads(it["full"])

    def fb_lines(kinds):
        rows = sorted(
            ((k, v) for k, v in feedback.items() if v.get("d") in kinds),
            key=lambda kv: kv[1].get("t", ""), reverse=True,
        )[:25]
        return [f"- {k}: {v.get('c') or ''} {v.get('s') or ''}".strip() for k, v in rows]

    ai = gemini_summaries(items, fb_lines({"use", "dev"}), fb_lines({"no"}))

    top_buzz = max((it["buzz"] for it in items), default=1) or 1
    for it in items:
        it.pop("_readme", None)
        a = ai.get(it["full"].lower())
        if a:
            it["ai"] = {key: a.get(key) for key in
                        ("cim", "mit_csinal", "mire_jo", "kategoria", "telefonrol", "egyezes", "miert")}
            match = float(a.get("egyezes") or 0)
            it["score"] = round(0.6 * match + 40 * it["buzz"] / top_buzz, 1)
        else:
            it["score"] = round(100 * it["buzz"] / top_buzz, 1)
        history[it["full"].lower()]["shown"] = TODAY
    items.sort(key=lambda x: x["score"], reverse=True)

    # értékeletlen tételek pár napig maradnak, nem vesznek el
    new_keys = {it["full"].lower() for it in items}
    keep_from = days_ago(CFG.get("keep_unrated_days", 3))
    carried = [
        dict(it, old=True) for it in previous.get("items", [])
        if it["full"].lower() not in rated
        and it["full"].lower() not in new_keys
        and it.get("first_seen", TODAY) >= keep_from
    ]

    # előzmények karbantartása
    prune = days_ago(60)
    for k in list(history):
        e = history[k]
        if e.get("seen", "") < prune and e.get("shown", "") < prune:
            del history[k]
            continue
        e["stars"] = dict(sorted(e.get("stars", {}).items())[-8:])

    save("repos.json", {
        "date": TODAY,
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "model": GEMINI_MODEL if ai else "",
        "items": items + carried,
    })
    save("history.json", history)
    print(f"Kész: {len(items)} új + {len(carried)} korábbi, értékeletlen tétel.")


if __name__ == "__main__":
    main()
