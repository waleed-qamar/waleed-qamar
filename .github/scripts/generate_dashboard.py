#!/usr/bin/env python3
"""
Generates a single composite GitHub activity dashboard as an SVG.

Queries the GitHub GraphQL API, computes stats, and renders a self-contained
SVG (fonts are system stacks, the avatar is inlined as a data URI) so it works
inside a README with no external requests at view time.

Usage:
    GH_TOKEN=ghp_xxx python3 generate_dashboard.py --user waleed-qamar --out assets
    python3 generate_dashboard.py --mock --out /tmp        # no network, fake data
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import math
import os
import sys
import urllib.request
from collections import Counter, defaultdict

API = "https://api.github.com/graphql"

# --------------------------------------------------------------------------
# Palette — anchored to hero.svg so the profile reads as one piece.
# --------------------------------------------------------------------------

THEMES = {
    "dark": {
        "bg": "#0E1116",
        "panel": "#141920",
        "panel_alt": "#191F27",
        "border": "#22262C",
        "text": "#F3F6F9",
        "muted": "#8B95A1",
        "faint": "#5A6470",
        "accent": "#54C5F8",
        "grid": "#1B212A",
        "heat": ["#161B22", "#0E4B66", "#12708F", "#2B9DC7", "#54C5F8"],
    },
    "light": {
        "bg": "#FFFFFF",
        "panel": "#F7F9FB",
        "panel_alt": "#EEF2F6",
        "border": "#D8DEE6",
        "text": "#10141A",
        "muted": "#5A6470",
        "faint": "#8B95A1",
        "accent": "#1B84B8",
        "grid": "#E3E9EF",
        "heat": ["#EBEDF0", "#C7E9F8", "#8FD3F0", "#4FAFDA", "#1B84B8"],
    },
}

SERIES = ["#54C5F8", "#7C5CFF", "#4CD97B", "#F5A524", "#FF6B6B", "#8B95A1"]

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "'SF Mono',Menlo,Consolas,'DejaVu Sans Mono',monospace"

W = 1100  # canvas width

# --------------------------------------------------------------------------
# GraphQL
# --------------------------------------------------------------------------

QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    name
    login
    avatarUrl(size: 160)
    createdAt
    followers { totalCount }
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalPullRequestContributions
      totalIssueContributions
      totalRepositoriesWithContributedCommits
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays { date contributionCount weekday }
        }
      }
    }
    repositories(
      first: 100
      isFork: false
      ownerAffiliations: OWNER
      orderBy: { field: PUSHED_AT, direction: DESC }
    ) {
      nodes {
        name
        stargazerCount
        primaryLanguage { name color }
        languages(first: 8, orderBy: { field: SIZE, direction: DESC }) {
          edges { size node { name color } }
        }
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 100, since: $from) {
                totalCount
                nodes { committedDate }
              }
            }
          }
        }
      }
    }
  }
}
"""


def graphql(token: str, login: str) -> dict:
    to = dt.datetime.now(dt.timezone.utc)
    frm = to - dt.timedelta(days=365)
    body = json.dumps(
        {
            "query": QUERY,
            "variables": {
                "login": login,
                "from": frm.isoformat(),
                "to": to.isoformat(),
            },
        }
    ).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-dashboard-generator",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = json.load(r)
    if "errors" in payload:
        raise SystemExit("GraphQL errors:\n" + json.dumps(payload["errors"], indent=2))
    if not payload.get("data", {}).get("user"):
        raise SystemExit(f"No such user: {login}")
    return payload["data"]["user"]


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------


def build_stats(user: dict, utc_offset: int) -> dict:
    cc = user["contributionsCollection"]
    cal = cc["contributionCalendar"]

    days = []
    for week in cal["weeks"]:
        for d in week["contributionDays"]:
            days.append((d["date"], d["contributionCount"]))
    days.sort()

    # Current streak: walk backwards, allowing today to be empty (day isn't over).
    streak = 0
    for i, (_, count) in enumerate(reversed(days)):
        if count > 0:
            streak += 1
        elif i == 0:
            continue
        else:
            break

    repos = [r for r in user["repositories"]["nodes"] if r]

    by_repo = Counter()
    repo_colors = {}
    by_bytes = Counter()
    byte_colors = {}
    stars = 0
    hours = [0] * 24

    for r in repos:
        stars += r["stargazerCount"]
        pl = r.get("primaryLanguage")
        if pl:
            by_repo[pl["name"]] += 1
            repo_colors[pl["name"]] = pl["color"]
        for edge in (r.get("languages") or {}).get("edges", []):
            node = edge["node"]
            by_bytes[node["name"]] += edge["size"]
            byte_colors[node["name"]] = node["color"]
        ref = r.get("defaultBranchRef") or {}
        target = ref.get("target") or {}
        for c in (target.get("history") or {}).get("nodes", []) or []:
            ts = dt.datetime.fromisoformat(c["committedDate"].replace("Z", "+00:00"))
            hours[(ts.hour + utc_offset) % 24] += 1

    return {
        "name": user.get("name") or user["login"],
        "login": user["login"],
        "avatar_url": user["avatarUrl"],
        "joined": dt.datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00")),
        "followers": user["followers"]["totalCount"],
        "streak": streak,
        "total_contributions": cal["totalContributions"],
        "commits": cc["totalCommitContributions"],
        "prs": cc["totalPullRequestContributions"],
        "issues": cc["totalIssueContributions"],
        "contributed_to": cc["totalRepositoriesWithContributedCommits"],
        "stars": stars,
        "repo_count": len(repos),
        "code_size": human_bytes(sum(by_bytes.values())),
        "lang_by_repo": top_n(by_repo, repo_colors),
        "lang_by_bytes": top_n(by_bytes, byte_colors),
        "hours": hours,
        "days": days,
    }


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def top_n(counter: Counter, colors: dict, n: int = 5) -> list:
    total = sum(counter.values()) or 1
    out = []
    for i, (name, val) in enumerate(counter.most_common(n)):
        out.append(
            {
                "name": name,
                "pct": val / total * 100,
                "color": colors.get(name) or SERIES[i % len(SERIES)],
            }
        )
    return out


def fetch_avatar(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dashboard"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        return base64.b64encode(raw).decode()
    except Exception as e:  # noqa: BLE001
        print(f"  avatar fetch failed ({e}) — falling back to initials", file=sys.stderr)
        return None


# --------------------------------------------------------------------------
# SVG primitives
# --------------------------------------------------------------------------


def esc(s) -> str:
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def text(x, y, s, size=13, fill="#fff", weight=400, anchor="start",
         family=FONT, spacing=None, opacity=None) -> str:
    attrs = [
        f'x="{x}"', f'y="{y}"', f'font-family="{family}"',
        f'font-size="{size}"', f'font-weight="{weight}"', f'fill="{fill}"',
    ]
    if anchor != "start":
        attrs.append(f'text-anchor="{anchor}"')
    if spacing is not None:
        attrs.append(f'letter-spacing="{spacing}"')
    if opacity is not None:
        attrs.append(f'opacity="{opacity}"')
    return f'<text {" ".join(attrs)}>{esc(s)}</text>'


def panel(x, y, w, h, t, r=14, fill=None) -> str:
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" '
        f'fill="{fill or t["panel"]}" stroke="{t["border"]}" stroke-width="1"/>'
    )


def heading(x, y, label, t) -> str:
    """Section heading with an accent tick instead of an emoji icon."""
    return (
        f'<rect x="{x}" y="{y - 10}" width="3" height="13" rx="1.5" fill="{t["accent"]}"/>'
        + text(x + 11, y, label.upper(), size=11, fill=t["muted"], weight=700, spacing=1.4)
    )


def donut(cx, cy, r, thickness, slices, t) -> str:
    """Ring chart via stroke-dasharray. Rotated so slice 1 starts at 12 o'clock."""
    circ = 2 * math.pi * r
    out = [
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" '
        f'stroke="{t["grid"]}" stroke-width="{thickness}"/>'
    ]
    offset = 0.0
    for s in slices:
        seg = circ * s["pct"] / 100
        if seg <= 0:
            continue
        out.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" '
            f'stroke="{s["color"]}" stroke-width="{thickness}" '
            f'stroke-dasharray="{seg:.2f} {circ - seg:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 {cx} {cy})" stroke-linecap="butt"/>'
        )
        offset += seg
    return "".join(out)


def legend(x, y, slices, t, row_h=25, bar_w=78) -> str:
    out = []
    for i, s in enumerate(slices):
        yy = y + i * row_h
        out.append(f'<circle cx="{x + 4}" cy="{yy - 4}" r="4" fill="{s["color"]}"/>')
        out.append(text(x + 16, yy, s["name"], size=12.5, fill=t["text"], weight=500))
        out.append(
            text(x + 205, yy, f'{s["pct"]:.1f}%', size=12, fill=t["muted"],
                 weight=600, anchor="end", family=MONO)
        )
        # proportional bar, normalised against the largest slice
        top = max(sl["pct"] for sl in slices) or 1
        w = max(3, bar_w * s["pct"] / top)
        out.append(
            f'<rect x="{x + 215}" y="{yy - 8}" width="{bar_w}" height="5" rx="2.5" fill="{t["grid"]}"/>'
        )
        out.append(
            f'<rect x="{x + 215}" y="{yy - 8}" width="{w:.1f}" height="5" rx="2.5" fill="{s["color"]}"/>'
        )
    return "".join(out)


def stat_tile(x, y, w, h, value, label, t) -> str:
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
        f'fill="{t["panel_alt"]}" stroke="{t["border"]}" stroke-width="1"/>'
        + text(x + w / 2, y + h / 2 + 1, fmt(value), size=21, fill=t["text"],
               weight=700, anchor="middle")
        + text(x + w / 2, y + h - 13, label.upper(), size=9, fill=t["faint"],
               weight=600, anchor="middle", spacing=0.8)
    )


def fmt(n) -> str:
    n = int(n)
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


def header(x, y, w, h, st, avatar_b64, t) -> str:
    out = [panel(x, y, w, h, t)]
    ax, ay, ar = x + 26, y + h / 2, 30

    out.append(f'<circle cx="{ax + ar}" cy="{ay}" r="{ar + 3}" fill="none" '
               f'stroke="{t["accent"]}" stroke-width="1.5" opacity="0.55"/>')
    if avatar_b64:
        out.append(f'<clipPath id="av"><circle cx="{ax + ar}" cy="{ay}" r="{ar}"/></clipPath>')
        out.append(
            f'<image x="{ax}" y="{ay - ar}" width="{ar * 2}" height="{ar * 2}" '
            f'clip-path="url(#av)" preserveAspectRatio="xMidYMid slice" '
            f'href="data:image/png;base64,{avatar_b64}" '
            f'xlink:href="data:image/png;base64,{avatar_b64}"/>'
        )
    else:
        out.append(f'<circle cx="{ax + ar}" cy="{ay}" r="{ar}" fill="{t["panel_alt"]}"/>')
        out.append(text(ax + ar, ay + 9, st["name"][0].upper(), size=26,
                        fill=t["accent"], weight=800, anchor="middle"))

    tx = ax + ar * 2 + 22
    out.append(
        f'<text x="{tx}" y="{y + 44}" font-family="{FONT}" font-size="27" font-weight="700">'
        f'<tspan fill="{t["text"]}">Hi, I\'m </tspan>'
        f'<tspan fill="{t["accent"]}">{esc(st["name"])}</tspan></text>'
    )
    out.append(text(tx, y + 68, "Flutter engineer building systems worth trusting.",
                    size=13.5, fill=t["muted"], weight=400))

    # right-hand key figures
    chips = [
        ("JOINED GITHUB", st["joined"].strftime("%b %Y")),
        ("CURRENT STREAK", f'{st["streak"]} day' + ("s" if st["streak"] != 1 else "")),
        ("CONTRIBUTIONS", fmt(st["total_contributions"])),
    ]
    cw, gap = 132, 12
    cx = x + w - 26 - (cw * len(chips) + gap * (len(chips) - 1))
    for label, value in chips:
        out.append(f'<rect x="{cx}" y="{y + 24}" width="{cw}" height="56" rx="10" '
                   f'fill="{t["panel_alt"]}" stroke="{t["border"]}"/>')
        out.append(text(cx + cw / 2, y + 45, label, size=8.5, fill=t["faint"],
                        weight=700, anchor="middle", spacing=0.9))
        out.append(text(cx + cw / 2, y + 66, value, size=15, fill=t["accent"],
                        weight=700, anchor="middle"))
        cx += cw + gap
    return "".join(out)


def lang_panel(x, y, w, h, title, slices, t, centre=("", "")) -> str:
    out = [panel(x, y, w, h, t), heading(x + 22, y + 30, title, t)]
    if not slices:
        out.append(text(x + w / 2, y + h / 2, "No language data yet",
                        size=12, fill=t["faint"], anchor="middle"))
        return "".join(out)
    out.append(donut(x + 92, y + h / 2 + 14, 46, 20, slices, t))
    out.append(text(x + 92, y + h / 2 + 19, centre[0], size=17, fill=t["text"],
                    weight=700, anchor="middle"))
    out.append(text(x + 92, y + h / 2 + 33, centre[1], size=7.5, fill=t["faint"],
                    weight=700, anchor="middle", spacing=0.9))
    out.append(legend(x + 172, y + 62, slices, t))
    return "".join(out)


def overview_panel(x, y, w, h, st, t) -> str:
    out = [panel(x, y, w, h, t), heading(x + 22, y + 30, "Repository Overview", t)]
    tiles = [
        (st["stars"], "Total stars"),
        (st["commits"], "Commits (1y)"),
        (st["prs"], "Pull requests"),
        (st["repo_count"], "Public repos"),
        (st["contributed_to"], "Contributed to"),
        (st["followers"], "Followers"),
    ]
    tw, th, gx, gy = (w - 44 - 2 * 12) / 3, 62, 12, 12
    for i, (val, label) in enumerate(tiles):
        col, row = i % 3, i // 3
        out.append(stat_tile(x + 22 + col * (tw + gx), y + 48 + row * (th + gy),
                             tw, th, val, label, t))
    return "".join(out)


def hours_panel(x, y, w, h, st, t, utc_offset) -> str:
    out = [
        panel(x, y, w, h, t),
        heading(x + 22, y + 30, "When I commit", t),
    ]
    hours = st["hours"]
    peak = max(hours) or 1
    out.append(text(x + w - 22, y + 30,
                    f"PEAK {hours.index(peak):02d}:00 · UTC+{utc_offset}",
                    size=9.5, fill=t["faint"], weight=700, anchor="end",
                    family=MONO, spacing=0.6))
    plot_x, plot_y = x + 30, y + 52
    plot_w, plot_h = w - 52, h - 92
    bw = plot_w / 24

    for gl in (0, 0.5, 1):
        gy = plot_y + plot_h * gl
        out.append(f'<line x1="{plot_x}" y1="{gy:.1f}" x2="{plot_x + plot_w}" '
                   f'y2="{gy:.1f}" stroke="{t["grid"]}" stroke-width="1"/>')

    busiest = hours.index(peak)
    for i, v in enumerate(hours):
        bh = max(2, plot_h * v / peak)
        bx = plot_x + i * bw + 1.5
        colour = t["accent"] if i == busiest else t["accent"]
        op = 1.0 if i == busiest else 0.42
        out.append(f'<rect x="{bx:.1f}" y="{plot_y + plot_h - bh:.1f}" '
                   f'width="{bw - 3:.1f}" height="{bh:.1f}" rx="2" '
                   f'fill="{colour}" opacity="{op}"/>')

    for hh in (0, 6, 12, 18, 23):
        out.append(text(plot_x + hh * bw + bw / 2, plot_y + plot_h + 16,
                        f"{hh:02d}", size=9.5, fill=t["faint"], weight=600,
                        anchor="middle", family=MONO))
    return "".join(out)


def heatmap_panel(x, y, w, h, st, t) -> str:
    out = [panel(x, y, w, h, t), heading(x + 22, y + 30, "Contribution Activity", t)]
    days = st["days"]
    if not days:
        return "".join(out)

    cell, gap = 15, 3
    step = cell + gap
    grid_x, grid_y = x + 54, y + 62

    first = dt.date.fromisoformat(days[0][0])
    origin = first - dt.timedelta(days=(first.weekday() + 1) % 7)

    # Bucket on quantiles of active days, not the max, so one outlier day
    # doesn't flatten the whole year into the palest shade.
    active = sorted(c for _, c in days if c > 0)
    if active:
        cuts = [active[int(len(active) * q) - 1] for q in (0.35, 0.65, 0.88)]
    else:
        cuts = [1, 2, 3]

    month_seen = set()
    for date_str, count in days:
        d = dt.date.fromisoformat(date_str)
        col = (d - origin).days // 7
        row = (d.weekday() + 1) % 7
        if count == 0:
            idx = 0
        elif count <= cuts[0]:
            idx = 1
        elif count <= cuts[1]:
            idx = 2
        elif count <= cuts[2]:
            idx = 3
        else:
            idx = 4
        out.append(
            f'<rect x="{grid_x + col * step}" y="{grid_y + row * step}" '
            f'width="{cell}" height="{cell}" rx="3" fill="{t["heat"][idx]}"/>'
        )
        if d.day <= 7 and d.month not in month_seen and col > 0:
            month_seen.add(d.month)
            out.append(text(grid_x + col * step, grid_y - 10,
                            d.strftime("%b"), size=9.5, fill=t["faint"], weight=600))

    for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        out.append(text(x + 22, grid_y + row * step + 11, label, size=9.5,
                        fill=t["faint"], weight=600))

    foot = grid_y + 7 * step + 20
    out.append(text(x + 54, foot,
                    f'{fmt(st["total_contributions"])} contributions in the last year',
                    size=11.5, fill=t["muted"], weight=500))

    lx = x + w - 172
    out.append(text(lx, foot, "Less", size=9.5, fill=t["faint"], weight=600))
    for i, c in enumerate(t["heat"]):
        out.append(f'<rect x="{lx + 32 + i * 17}" y="{foot - 10}" width="12" '
                   f'height="12" rx="3" fill="{c}"/>')
    out.append(text(lx + 124, foot, "More", size=9.5, fill=t["faint"], weight=600))
    return "".join(out)


# --------------------------------------------------------------------------
# Compose
# --------------------------------------------------------------------------


def render(st: dict, avatar_b64, theme_name: str, utc_offset: int) -> str:
    t = THEMES[theme_name]
    pad, gap = 18, 14
    half = (W - pad * 2 - gap) / 2

    y = pad
    body = []

    body.append(header(pad, y, W - pad * 2, 104, st, avatar_b64, t))
    y += 104 + gap

    body.append(lang_panel(pad, y, half, 196, "Top languages by repository",
                           st["lang_by_repo"], t,
                           centre=(fmt(st["repo_count"]), "REPOS")))
    body.append(lang_panel(pad + half + gap, y, half, 196, "Top languages by code volume",
                           st["lang_by_bytes"], t,
                           centre=(st["code_size"], "OF CODE")))
    y += 196 + gap

    body.append(overview_panel(pad, y, half, 184, st, t))
    body.append(hours_panel(pad + half + gap, y, half, 184, st, t, utc_offset))
    y += 184 + gap

    body.append(heatmap_panel(pad, y, W - pad * 2, 226, st, t))
    y += 226 + pad

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{W}" height="{int(y)}" viewBox="0 0 {W} {int(y)}" '
        f'role="img" aria-label="GitHub activity dashboard for {esc(st["login"])}">'
        f'<rect width="{W}" height="{int(y)}" rx="18" fill="{t["bg"]}"/>'
        + "".join(body)
        + "</svg>"
    )


# --------------------------------------------------------------------------


def mock_user() -> dict:
    import random

    random.seed(7)
    to = dt.datetime.now(dt.timezone.utc)
    weeks = []
    d = (to - dt.timedelta(days=364)).date()
    d -= dt.timedelta(days=(d.weekday() + 1) % 7)
    while d <= to.date():
        wk = []
        for _ in range(7):
            if d > to.date():
                break
            wk.append({"date": d.isoformat(),
                       "contributionCount": random.choice([0, 0, 0, 1, 2, 3, 5, 8, 13]),
                       "weekday": (d.weekday() + 1) % 7})
            d += dt.timedelta(days=1)
        weeks.append({"contributionDays": wk})

    langs = [("Dart", "#00B4AB"), ("Python", "#3572A5"), ("C++", "#f34b7d"),
             ("JavaScript", "#f1e05a"), ("HTML", "#e34c26")]
    repos = []
    for i in range(24):
        primary = langs[i % len(langs)]
        repos.append({
            "name": f"repo-{i}",
            "stargazerCount": random.choice([0, 0, 1, 2, 5, 19]),
            "primaryLanguage": {"name": primary[0], "color": primary[1]},
            "languages": {"edges": [
                {"size": random.randint(2000, 90000),
                 "node": {"name": n, "color": c}}
                for n, c in random.sample(langs, 3)
            ]},
            "defaultBranchRef": {"target": {"history": {
                "totalCount": 40,
                "nodes": [{"committedDate":
                           (to - dt.timedelta(days=random.randint(0, 300),
                                              hours=random.choice([2, 3, 4, 14, 15, 16, 19, 20, 21, 22, 22, 23])
                                              )).isoformat().replace("+00:00", "Z")}
                          for _ in range(40)]
            }}},
        })

    return {
        "name": "Waleed Qamar",
        "login": "waleed-qamar",
        "avatarUrl": "",
        "createdAt": "2023-01-14T00:00:00Z",
        "followers": {"totalCount": 38},
        "contributionsCollection": {
            "totalCommitContributions": 842,
            "totalPullRequestContributions": 9,
            "totalIssueContributions": 4,
            "totalRepositoriesWithContributedCommits": 22,
            "contributionCalendar": {
                "totalContributions": sum(
                    day["contributionCount"] for w in weeks for day in w["contributionDays"]
                ),
                "weeks": weeks,
            },
        },
        "repositories": {"nodes": repos},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=os.environ.get("GH_USER", ""))
    ap.add_argument("--out", default="assets")
    ap.add_argument("--utc-offset", type=int, default=5)
    ap.add_argument("--mock", action="store_true", help="render fake data, no network")
    args = ap.parse_args()

    if args.mock:
        user = mock_user()
        avatar = None
    else:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            raise SystemExit("Set GH_TOKEN (a PAT with read:user) or GITHUB_TOKEN.")
        if not args.user:
            raise SystemExit("Pass --user or set GH_USER.")
        print(f"Querying GitHub for {args.user} ...")
        user = graphql(token, args.user)
        avatar = fetch_avatar(user["avatarUrl"])

    st = build_stats(user, args.utc_offset)
    os.makedirs(args.out, exist_ok=True)

    for theme in ("dark", "light"):
        svg = render(st, avatar, theme, args.utc_offset)
        path = os.path.join(args.out, f"dashboard-{theme}.svg")
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg)
        print(f"  wrote {path}  ({len(svg) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
