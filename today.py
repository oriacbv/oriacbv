#!/usr/bin/env python3
"""
Genera light_mode.svg i dark_mode.svg: una targeta estil `neofetch` amb les
dades del perfil de GitHub i un retrat en ASCII.

Ús local:   python3 today.py
A la Action: s'executa cada dia amb el secret ACCESS_TOKEN.

Tot el que és "teu" i no surt de l'API és aquí sota, a CONFIG. Edita-ho i prou.
"""

import base64
import datetime as dt
import json
import os
import pathlib
import sys
import time

import requests
from PIL import Image, ImageDraw, ImageOps

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — l'única part que has de tocar
# ─────────────────────────────────────────────────────────────────────────────

CONFIG = {
    "user": "oriacbv",
    "birthday": dt.date(1996, 4, 12),          # per al camp "Uptime"
    "ascii_file": "ascii_art.txt",              # si hi és, mana ell
    "avatar": "avatar.png",                     # si no, converteix aquesta foto
    "ascii_cols": 44,

    # Cada tupla és una línia de la targeta. ("", "") és una línia en blanc.
    # Revisa-les: me n'he inventat unes quantes de plausibles.
    "fields": [
        ("OS",                  "Linux · macOS quan hi ha reunió"),
        ("Host",                "Dribba"),
        ("Kernel",              "Go 1.23"),
        ("Uptime",              "{uptime}"),
        ("IDE",                 "VS Code · Neovim quan ningú mira"),
        ("", ""),
        ("Llenguatges.Codi",    "Go, SQL, Python, Bash"),
        ("Llenguatges.Humans",  "Català, Castellà, English"),
        ("Stack",               "PostgreSQL, Redis, Docker, gRPC"),
        ("Hobbies",             "Desplegar un divendres"),
        ("", ""),
        ("Contacte.GitHub",     "@oriacbv"),
        ("Contacte.LinkedIn",   "linkedin.com/in/oriacbv"),
        ("Contacte.Correu",     "oriac.bonvehi@dribba.com"),
        ("", ""),
        ("GitHub.Repos",        "{repos} · {contributed} de tercers"),
        ("GitHub.Commits",      "{commits}"),
        ("GitHub.Estrelles",    "{stars}"),
        ("GitHub.Seguidors",    "{followers}"),
        ("GitHub.Línies",       "{loc} ({added}++, {deleted}--)"),
    ],
}

THEMES = {
    "light_mode.svg": {
        "bg": "#ffffff", "panel": "#f6f8fa", "border": "#d0d7de",
        "fg": "#1f2328", "dim": "#59636e",
        "accent": "#00875a", "key": "#0550ae", "art": "#00875a",
    },
    "dark_mode.svg": {
        "bg": "#0d1117", "panel": "#151b23", "border": "#30363d",
        "fg": "#c9d1d9", "dim": "#8b949e",
        "accent": "#3fbf8b", "key": "#79c0ff", "art": "#3fbf8b",
    },
}

ROOT = pathlib.Path(__file__).parent
CACHE = ROOT / "cache"
API = "https://api.github.com"
RAMP = "@%#*+=-:. "  # de més fosc a més clar

# ─────────────────────────────────────────────────────────────────────────────
# Dades de GitHub
# ─────────────────────────────────────────────────────────────────────────────


def session():
    token = os.environ.get("ACCESS_TOKEN", "")
    s = requests.Session()
    s.headers.update({
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{CONFIG['user']}-profile-card",
    })
    if token:
        s.headers["Authorization"] = f"bearer {token}"
    return s


def graphql(s, query, variables):
    r = s.post("https://api.github.com/graphql",
               json={"query": query, "variables": variables}, timeout=30)
    r.raise_for_status()
    payload = r.json()
    if "errors" in payload:
        raise RuntimeError(payload["errors"])
    return payload["data"]


USER_Q = """
query($login:String!, $cursor:String) {
  user(login:$login) {
    createdAt
    followers { totalCount }
    repositoriesContributedTo(first:1, contributionTypes:[COMMIT],
                              includeUserRepositories:false) { totalCount }
    repositories(first:100, after:$cursor, ownerAffiliations:[OWNER],
                 isFork:false, orderBy:{field:PUSHED_AT, direction:DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { nameWithOwner stargazerCount pushedAt isEmpty }
    }
  }
}
"""

COMMITS_Q = """
query($login:String!, $from:DateTime!, $to:DateTime!) {
  user(login:$login) {
    contributionsCollection(from:$from, to:$to) {
      totalCommitContributions
      restrictedContributionsCount
    }
  }
}
"""


def fetch_profile(s):
    login = CONFIG["user"]
    repos, cursor, first = [], None, None
    while True:
        data = graphql(s, USER_Q, {"login": login, "cursor": cursor})["user"]
        first = first or data
        block = data["repositories"]
        repos += [n for n in block["nodes"] if not n["isEmpty"]]
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]

    created = dt.datetime.fromisoformat(first["createdAt"].replace("Z", "+00:00"))
    commits = 0
    year = created.year
    now = dt.datetime.now(dt.timezone.utc)
    while year <= now.year:
        frm = max(created, dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc))
        to = min(now, dt.datetime(year, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc))
        c = graphql(s, COMMITS_Q, {
            "login": login, "from": frm.isoformat(), "to": to.isoformat(),
        })["user"]["contributionsCollection"]
        commits += c["totalCommitContributions"] + c["restrictedContributionsCount"]
        year += 1

    return {
        "repos": first["repositories"]["totalCount"],
        "contributed": first["repositoriesContributedTo"]["totalCount"],
        "followers": first["followers"]["totalCount"],
        "stars": sum(r["stargazerCount"] for r in repos),
        "commits": commits,
        "repo_list": repos,
    }


def fetch_loc(s, repos):
    """Línies afegides i esborrades, via l'endpoint d'estadístiques. Amb cache
    per `pushedAt`: un repo que no s'ha mogut no es torna a demanar."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / "loc.json"
    cache = json.loads(path.read_text()) if path.exists() else {}
    added = deleted = 0

    for repo in repos:
        name = repo["nameWithOwner"]
        hit = cache.get(name)
        if hit and hit.get("pushedAt") == repo["pushedAt"]:
            added += hit["added"]
            deleted += hit["deleted"]
            continue

        a = d = 0
        for attempt in range(4):
            r = s.get(f"{API}/repos/{name}/stats/contributors", timeout=30)
            if r.status_code == 202:      # GitHub encara les està calculant
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 204 or not r.content:
                break
            r.raise_for_status()
            for entry in r.json() or []:
                if (entry.get("author") or {}).get("login", "").lower() != CONFIG["user"].lower():
                    continue
                for week in entry["weeks"]:
                    a += week["a"]
                    d += week["d"]
            break

        cache[name] = {"pushedAt": repo["pushedAt"], "added": a, "deleted": d}
        added += a
        deleted += d

    path.write_text(json.dumps(cache, indent=1, sort_keys=True))
    return added, deleted


def uptime_text():
    today = dt.date.today()
    b = CONFIG["birthday"]
    years = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
    anniversary = dt.date(today.year - (1 if (today.month, today.day) < (b.month, b.day) else 0),
                          b.month, b.day)
    days = (today - anniversary).days
    months, days = divmod(days, 30)
    parts = [f"{years} anys", f"{months} mes" + ("os" if months != 1 else ""),
             f"{days} dia" + ("s" if days != 1 else "")]
    return ", ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Retrat en ASCII
# ─────────────────────────────────────────────────────────────────────────────


def placeholder_avatar(size=400):
    """Silueta de mostra, per quan encara no hi ha foto."""
    img = Image.new("L", (size, size), 235)
    d = ImageDraw.Draw(img)
    c = size // 2
    d.ellipse([c - size * .30, size * .34, c + size * .30, size * 1.05], fill=90)
    d.ellipse([c - size * .19, size * .12, c + size * .19, size * .52], fill=60)
    d.ellipse([c - size * .13, size * .30, c + size * .13, size * .44], fill=110)
    return img


def crop_frame(lines):
    """Treu el marc uniforme de @ i d'espais que envolta el retrat."""
    width = max(len(l) for l in lines)
    grid = [l.ljust(width) for l in lines]
    flat = lambda seq: len(set(seq)) == 1 and seq[0] in "@ "
    while len(grid) > 1 and flat(grid[0]):
        grid.pop(0)
    while len(grid) > 1 and flat(grid[-1]):
        grid.pop()
    cols = list(zip(*grid))
    while len(cols) > 1 and flat(cols[0]):
        cols.pop(0)
    while len(cols) > 1 and flat(cols[-1]):
        cols.pop()
    return ["".join(row).rstrip() for row in zip(*cols)]


def load_ascii():
    handmade = ROOT / CONFIG.get("ascii_file", "")
    if CONFIG.get("ascii_file") and handmade.exists():
        return crop_frame(handmade.read_text(encoding="utf-8").splitlines())
    return to_ascii(CONFIG["avatar"], CONFIG["ascii_cols"])


def to_ascii(path, cols):
    if path and pathlib.Path(path).exists():
        img = Image.open(path)
    else:
        url = f"https://github.com/{CONFIG['user']}.png"
        try:
            raw = requests.get(url, timeout=20)
            raw.raise_for_status()
            import io
            img = Image.open(io.BytesIO(raw.content))
        except Exception:
            img = placeholder_avatar()

    img = img.convert("L")
    img = ImageOps.autocontrast(img, cutoff=2)
    w, h = img.size
    rows = max(1, round(cols * (h / w) * 0.48))   # els caràcters són ~2:1
    img = img.resize((cols, rows), Image.LANCZOS)

    px = img.load()
    lines = []
    for y in range(rows):
        line = "".join(RAMP[min(len(RAMP) - 1, px[x, y] * len(RAMP) // 256)]
                       for x in range(cols))
        lines.append(line.rstrip())
    return lines


# ─────────────────────────────────────────────────────────────────────────────
# SVG
# ─────────────────────────────────────────────────────────────────────────────

PAD = 26
BAR = 38
ART_SIZE = 10.0
ART_LH = 10.2
TXT_SIZE = 14.5
TXT_LH = 20.0
VALUE_X = 186
FONT = "'Cascadia Code','Fira Code',Consolas,'Andale Mono','DejaVu Sans Mono',monospace"


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_svg(theme, art, lines, title):
    char = TXT_SIZE * 0.60
    art_cols = max(len(l) for l in art)
    art_w = int(art_cols * ART_SIZE * 0.60) + 30
    info_x = PAD + art_w
    longest = max((len(v) for _, v, k in lines if k == "kv"), default=30)
    body_h = max(len(art) * ART_LH, len(lines) * TXT_LH)
    width = int(info_x + VALUE_X + longest * char + PAD)
    height = int(BAR + PAD + body_h + PAD)
    art_top = BAR + PAD + max(0, (body_h - len(art) * ART_LH) / 2)

    o = []
    o.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}" font-family="{FONT}" role="img" '
             f'aria-label="{esc(title)}">')
    o.append(f"""<style>
    text {{ white-space: pre; dominant-baseline: middle; }}
    .row {{ opacity: 0; animation: in .32s ease forwards; }}
    .art {{ opacity: 0; animation: in .5s ease forwards; }}
    @keyframes in {{ to {{ opacity: 1 }} }}
    .cursor {{ animation: blink 1.06s steps(1) infinite; }}
    @keyframes blink {{ 50% {{ opacity: 0 }} }}
    @media (prefers-reduced-motion: reduce) {{
      .row, .art {{ animation: none; opacity: 1 }}
      .cursor {{ animation: none }}
    }}
  </style>""")

    o.append(f'<rect width="{width}" height="{height}" rx="12" fill="{theme["bg"]}" '
             f'stroke="{theme["border"]}"/>')
    o.append(f'<path d="M0 {BAR} H{width}" stroke="{theme["border"]}"/>')
    o.append(f'<rect width="{width}" height="{BAR}" rx="12" fill="{theme["panel"]}"/>')
    o.append(f'<rect y="{BAR-12}" width="{width}" height="12" fill="{theme["panel"]}"/>')
    for i, col in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        o.append(f'<circle cx="{22 + i*17}" cy="{BAR/2}" r="5.5" fill="{col}" opacity=".9"/>')
    o.append(f'<text x="{width/2}" y="{BAR/2}" text-anchor="middle" font-size="12.5" '
             f'fill="{theme["dim"]}">{esc(title)}</text>')

    y0 = art_top + ART_LH / 2
    for i, line in enumerate(art):
        o.append(f'<text class="art" style="animation-delay:{i*0.022:.3f}s" x="{PAD}" '
                 f'y="{y0 + i*ART_LH:.1f}" font-size="{ART_SIZE}" fill="{theme["art"]}" '
                 f'xml:space="preserve">{esc(line)}</text>')

    y1 = BAR + PAD + TXT_LH / 2
    for i, (key, value, kind) in enumerate(lines):
        y = y1 + i * TXT_LH
        delay = 0.25 + i * 0.045
        if kind == "rule":
            o.append(f'<path class="row" style="animation-delay:{delay:.3f}s" '
                     f'd="M{info_x} {y:.1f} H{width - PAD}" '
                     f'stroke="{theme["border"]}"/>')
            continue
        if kind == "head":
            o.append(f'<text class="row" style="animation-delay:{delay:.3f}s" x="{info_x}" '
                     f'y="{y:.1f}" font-size="{TXT_SIZE}" font-weight="700" '
                     f'fill="{theme["accent"]}">{esc(key)}</text>')
            continue
        if not key and not value:
            continue
        o.append(f'<text class="row" style="animation-delay:{delay:.3f}s" x="{info_x}" '
                 f'y="{y:.1f}" font-size="{TXT_SIZE}" fill="{theme["key"]}">{esc(key)}</text>')
        o.append(f'<text class="row" style="animation-delay:{delay:.3f}s" '
                 f'x="{info_x + VALUE_X}" y="{y:.1f}" font-size="{TXT_SIZE}" '
                 f'fill="{theme["fg"]}">{esc(value)}</text>')

    last_y = y1 + (len(lines) - 1) * TXT_LH
    o.append(f'<rect class="cursor" x="{info_x}" y="{last_y + TXT_LH - 7:.1f}" width="9" '
             f'height="15" fill="{theme["accent"]}"/>')
    o.append("</svg>")
    return "\n".join(o)


def compose(stats):
    lines = [(f"{CONFIG['user']}@github", "", "head"), ("", "", "rule")]
    for key, value in CONFIG["fields"]:
        if not key:
            lines.append(("", "", "gap"))
        else:
            lines.append((key + ":", value.format(**stats), "kv"))
    return lines


def main():
    offline = "--offline" in sys.argv
    if offline:
        stats = {"repos": 24, "contributed": 6, "followers": 41, "stars": 118,
                 "commits": 2417, "loc": "184.902", "added": "212.660",
                 "deleted": "27.758"}
    else:
        s = session()
        p = fetch_profile(s)
        added, deleted = fetch_loc(s, p["repo_list"])
        fmt = lambda n: f"{n:,}".replace(",", ".")
        stats = {
            "repos": fmt(p["repos"]), "contributed": fmt(p["contributed"]),
            "followers": fmt(p["followers"]), "stars": fmt(p["stars"]),
            "commits": fmt(p["commits"]), "loc": fmt(added - deleted),
            "added": fmt(added), "deleted": fmt(deleted),
        }
    stats["uptime"] = uptime_text()

    art = load_ascii()
    lines = compose(stats)
    title = f"{CONFIG['user']} — neofetch"

    for filename, theme in THEMES.items():
        (ROOT / filename).write_text(build_svg(theme, art, lines, title), encoding="utf-8")
        print("escrit", filename)


if __name__ == "__main__":
    main()
