"""The report as a PDF (WeasyPrint: HTML and CSS to PDF, charts as inline SVG drawn here) and as a short caption.

Colours follow one rule: text stays in ink; a gain or a loss is a ▲ / ▼ marker in green or red next to the number,
so the sign never depends on colour alone. Lines are one blue, 2 px, with the last value labelled.
"""

import html
import math
from datetime import UTC, date
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment

from franziska.finance.service import eur
from franziska.invest.service import signed
from franziska.market import figures
from franziska.market.brief import SECTIONS, Brief, Point, short

INK, INK_2, MUTED, GRID, AXIS = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
BLUE, UP, DOWN = "#2a78d6", "#0ca30c", "#d03b3b"
TITLES = {"en": {"daily": "Daily market report", "weekly": "Weekly market report"},
          "it": {"daily": "Rapporto di mercato", "weekly": "Rapporto settimanale di mercato"}}
SOURCE_NAMES = {"yahoo": "Yahoo Finance", "ecb": "ECB", "fred": "FRED"}
TILES = ("S&P 500", "STOXX Europe 600", "FTSE MIB", "EUR/USD", "Brent crude", "US 10-year yield")


# ---- small formatters -----------------------------------------------------------------------------
def arrow(change: float | None, rate: bool = False) -> str:
    if change is None or abs(change) < (0.5 if rate else 0.005):  # what prints as 0 bp / 0.00% has no direction
        return f'<span class="mk" style="color:{MUTED}">•</span>'
    up = change > 0
    return f'<span class="mk" style="color:{UP if up else DOWN}">{"▲" if up else "▼"}</span>'


def pct(change: float | None, rate: bool = False) -> str:
    if change is None:
        return '<span class="na">–</span>'
    text = figures.fmt_change(change, rate)
    if text.lstrip("+-") in ("0 bp", "0.00%"):
        text = text.lstrip("+-")
    return f"{arrow(change, rate)}{text.replace('-', '−')}"


def money(cents: int | None, sign: bool = False) -> str:
    if cents is None:
        return "–"
    return (signed(cents) if sign else eur(cents)).replace("-", "−")


# ---- charts ---------------------------------------------------------------------------------------
def sparkline(rows: list[tuple[date, float]], w: int = 90, h: int = 22) -> str:
    vals = [v for _, v in rows[-23:]]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    pts = [(i * (w - 4) / (len(vals) - 1) + 2, h - 3 - (v - lo) / span * (h - 6)) for i, v in enumerate(vals)]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    x, y = pts[-1]
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}"><polyline points="{path}" fill="none" '
            f'stroke="{BLUE}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2" fill="{BLUE}"/></svg>')


def ticks(lo: float, hi: float, n: int = 3) -> list[float]:
    """About n round values inside [lo, hi]: steps of 1, 2, 2.5 or 5 times a power of ten."""
    raw = (hi - lo) / n
    power = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    step = next(m * power for m in (1, 2, 2.5, 5, 10) if m * power >= raw)
    first = math.ceil(lo / step) * step
    return [first + i * step for i in range(n + 1) if first + i * step <= hi]


def line_chart(series: list[tuple[date, int]], w: int = 680, h: int = 170) -> str:
    """The portfolio's value per session: one line, three gridlines, first / middle / last date, the last value."""
    if len(series) < 2:
        return ""
    left, right, top, bottom = 58, 70, 10, 22
    vals = [v / 100 for _, v in series]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or max(hi * 0.01, 1)
    lo, hi = lo - pad, hi + pad
    pw, ph = w - left - right, h - top - bottom

    def xy(i: int, v: float) -> tuple[float, float]:
        return left + i * pw / (len(vals) - 1), top + (hi - v) / (hi - lo) * ph

    out = [(f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" font-family="Noto Sans, DejaVu Sans" '
            f'font-size="9">')]
    for v in ticks(lo, hi):
        _, y = xy(0, v)
        out.append(f'<line x1="{left}" x2="{left + pw}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}" stroke-width="0.6"/>')
        out.append(f'<text x="{left - 6}" y="{y + 3:.1f}" text-anchor="end" fill="{MUTED}">'
                   f'{v:,.0f} €</text>'.replace(",", "'"))
    out.append(f'<line x1="{left}" x2="{left + pw}" y1="{top + ph}" y2="{top + ph}" stroke="{AXIS}" '
               f'stroke-width="0.8"/>')
    for i in sorted({0, len(series) // 2, len(series) - 1}):
        x, _ = xy(i, lo)
        anchor = "start" if i == 0 else "end" if i == len(series) - 1 else "middle"
        out.append(f'<text x="{x:.1f}" y="{h - 6}" text-anchor="{anchor}" fill="{MUTED}">'
                   f'{series[i][0]:%d %b}</text>')
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i, v) for i, v in enumerate(vals)))
    out.append(f'<polyline points="{path}" fill="none" stroke="{BLUE}" stroke-width="2" stroke-linejoin="round" '
               f'stroke-linecap="round"/>')
    x, y = xy(len(vals) - 1, vals[-1])
    out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{BLUE}" stroke="#fcfcfb" stroke-width="1.5"/>')
    out.append(f'<text x="{x + 6:.1f}" y="{y + 3:.1f}" fill="{INK}" font-weight="600">'
               f'{vals[-1]:,.0f} €</text>'.replace(",", "'"))
    out.append("</svg>")
    return "".join(out)


# ---- the page ---------------------------------------------------------------------------------------
TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
@page { size: A4; margin: 15mm 15mm 16mm 15mm; background: #fff;
  @bottom-left { content: "{{ footer }}"; font: 7pt 'Noto Sans', sans-serif; color: #898781; }
  @bottom-right { content: counter(page) " / " counter(pages); font: 7pt 'Noto Sans', sans-serif; color: #898781; } }
* { box-sizing: border-box; }
body { font-family: 'Noto Sans', 'Ubuntu Sans', 'DejaVu Sans', sans-serif; font-size: 9.2pt; line-height: 1.42;
  color: #0b0b0b; margin: 0; }
.top { display: flex; justify-content: space-between; align-items: baseline; border-bottom: 1.5px solid #0b0b0b;
  padding-bottom: 5px; margin-bottom: 10px; }
.top .kind { font-size: 8pt; letter-spacing: .08em; text-transform: uppercase; color: #52514e; font-weight: 600; }
.top .when { font-size: 8pt; color: #52514e; }
h1 { font-size: 17pt; line-height: 1.2; margin: 2px 0 6px; font-weight: 700; }
h2 { font-size: 10.5pt; margin: 16px 0 6px; padding-bottom: 3px; border-bottom: 0.6px solid #c3c2b7;
  text-transform: uppercase; letter-spacing: .05em; color: #0b0b0b; break-after: avoid; }
h3 { font-size: 9.5pt; margin: 10px 0 3px; break-after: avoid; }
p.lead { font-size: 10pt; color: #0b0b0b; margin: 0 0 10px; }
.warn { border: 1px solid #fab219; background: #fff8e6; padding: 6px 9px; border-radius: 4px; margin: 6px 0 10px;
  font-size: 8.5pt; }
.tiles { display: flex; gap: 6px; margin: 8px 0 4px; }
.tile { flex: 1; border: 0.6px solid #e1e0d9; border-radius: 6px; padding: 6px 7px; background: #fff; }
.tile .l { font-size: 7.2pt; color: #52514e; white-space: nowrap; overflow: hidden; }
.tile .v { font-size: 11.5pt; font-weight: 700; margin-top: 1px; white-space: nowrap; }
.tile .c { font-size: 7.8pt; color: #52514e; white-space: nowrap; }
.tile.me { border-color: #2a78d6; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th { text-align: right; font-weight: 600; font-size: 7.4pt; color: #52514e; border-bottom: 0.8px solid #c3c2b7;
  padding: 3px 4px; white-space: nowrap; }
td { text-align: right; padding: 3px 4px; border-bottom: 0.5px solid #e1e0d9; white-space: nowrap; font-size: 8.4pt; }
th:first-child, td:first-child { text-align: left; white-space: normal; }
tr.total td { font-weight: 700; border-top: 0.8px solid #c3c2b7; border-bottom: none; }
tr { break-inside: avoid; }
tr.group td { font-weight: 600; color: #52514e; font-size: 7.6pt; text-transform: uppercase; letter-spacing: .04em;
  padding-top: 7px; border-bottom: none; }
td.spark { padding: 0 4px; }
.mk { font-size: 6.8pt; margin-right: 2px; position: relative; top: -0.5px; }
.na { color: #898781; }
ul { margin: 2px 0 6px; padding-left: 14px; }
li { margin: 0 0 3.5px; }
sup.r { font-size: 6.5pt; color: #2a78d6; margin-left: 1px; }
.facts { color: #52514e; font-size: 8.2pt; margin: 5px 0 0; }
.note { color: #898781; font-size: 7.6pt; margin: 3px 0 0; }
.cal td { text-align: left; }
.cal td.d { width: 80px; color: #52514e; }
.src { font-size: 7.4pt; color: #52514e; }
.src li { margin-bottom: 2px; word-break: break-all; }
.src a { color: #2a78d6; text-decoration: none; }
.hl li { margin-bottom: 6px; }
.hl .m { color: #52514e; font-size: 7.8pt; }
.hl a { color: #0b0b0b; text-decoration: none; font-weight: 600; }
.chart { margin: 4px 0 2px; }
.cols { display: flex; gap: 18px; } .cols > div { flex: 1; }
</style></head><body>
<div class="top"><div class="kind">{{ title }}</div><div class="when">{{ subtitle }}</div></div>
{% if written %}<h1>{{ written.headline }}</h1><p class="lead">{{ written.summary }}</p>
{% else %}<h1>{{ fallback_title }}</h1>{% endif %}
{% for d in degraded %}<div class="warn">⚠️ {{ d }}</div>{% endfor %}
<div class="tiles">
{% if ov %}<div class="tile me"><div class="l">Your portfolio</div><div class="v">{{ ov_total }}</div>
<div class="c">{{ ov_day|safe }}{% if weekly %} · week {{ ov_period|safe }}{% endif %}</div></div>{% endif %}
{% for t in tiles %}<div class="tile"><div class="l">{{ t.label }}</div><div class="v">{{ t.value }}</div>
<div class="c">{{ t.change|safe }}</div></div>{% endfor %}
</div>
{% if ov %}
<h2>Your portfolio</h2>
{% if chart %}<div class="chart">{{ chart|safe }}</div>
<div class="note">Account value per session (positions at today's quantities, plus cash), last {{ chart_days }} sessions.</div>{% endif %}
<table><thead><tr><th>Holding</th><th>Value</th><th>Weight</th><th>Day</th><th>Day €</th>
{% if weekly %}<th>Week</th><th>Week €</th>{% endif %}<th>Month</th><th>Price</th></tr></thead><tbody>
{% for m in ov_rows %}<tr><td>{{ m.name }}</td><td>{{ m.value }}</td><td>{{ m.weight }}</td><td>{{ m.day|safe }}</td>
<td>{{ m.day_eur }}</td>{% if weekly %}<td>{{ m.period|safe }}</td><td>{{ m.period_eur }}</td>{% endif %}
<td>{{ m.month|safe }}</td><td class="src">{{ m.priced }}</td></tr>{% endfor %}
<tr><td>Cash (DEGIRO)</td><td>{{ ov_cash }}</td><td>{{ ov_cash_w }}</td><td></td><td></td>{% if weekly %}<td></td><td></td>{% endif %}<td></td><td></td></tr>
<tr class="total"><td>Total</td><td>{{ ov_total }}</td><td>100%</td><td>{{ ov_day_pct|safe }}</td><td>{{ ov_day_eur }}</td>
{% if weekly %}<td>{{ ov_period_pct|safe }}</td><td>{{ ov_period_eur }}</td>{% endif %}<td></td><td></td></tr>
</tbody></table>
<div class="facts">{{ ov_facts }}</div>
{% for n in ov_notes %}<div class="note">{{ n }}</div>{% endfor %}
{% endif %}
{% if written %}
{% for s in sections %}<h3>{{ s.title }}</h3><ul>{% for p in s.points %}<li>{{ p|safe }}</li>{% endfor %}</ul>{% endfor %}
{% if holdings %}<h2>Your holdings in the news</h2><ul>{% for p in holdings %}<li>{{ p|safe }}</li>{% endfor %}</ul>{% endif %}
{% endif %}
{% if headlines %}<h2>Headlines</h2><ol class="hl">{% for s in headlines %}
<li><a href="{{ s.url }}">{{ s.title }}</a><br><span class="m">{{ s.source }} · {{ s.when }}</span>
{% if s.summary %}<br>{{ s.summary }}{% endif %}</li>{% endfor %}</ol>{% endif %}
<h2>{{ 'The week ahead' if weekly else 'Coming up' }}</h2>
{% if calendar %}<table class="cal"><tbody>{% for c in calendar %}<tr><td class="d">{{ c.day }}</td><td>{{ c.title }}</td></tr>{% endfor %}</tbody></table>
{% else %}<div class="note">No central-bank decision or earnings of a holding in the next days.</div>{% endif %}
{% if watch %}<ul style="margin-top:6px">{% for p in watch %}<li>{{ p|safe }}</li>{% endfor %}</ul>{% endif %}
<h2>Markets</h2>
<table><thead><tr><th>Market</th><th>Last</th><th>Day</th><th>Week</th><th>Month</th><th>Last month</th><th>Source</th></tr></thead><tbody>
{% for g in groups %}<tr class="group"><td colspan="7">{{ g.name }}</td></tr>
{% for r in g.rows %}<tr><td>{{ r.label }}</td><td>{{ r.last }}</td><td>{{ r.d|safe }}</td><td>{{ r.w|safe }}</td><td>{{ r.m|safe }}</td>
<td class="spark">{{ r.spark|safe }}</td><td class="src">{{ r.source }}</td></tr>{% endfor %}{% endfor %}
</tbody></table>
<div class="note">Closes up to {{ closes_to }}. Yields change in basis points (bp). {{ notes_markets }}</div>
{% if sources %}<h2>Sources</h2><ol class="src">{% for s in sources %}
<li>{{ s.title }} — {{ s.source }}, {{ s.when }}{% if s.also %} (also: {{ s.also }}){% endif %}. <a href="{{ s.url }}">{{ s.short_url }}</a></li>{% endfor %}</ol>{% endif %}
<div class="note" style="margin-top:12px">{{ colophon }}</div>
</body></html>"""


def _point(p: Point) -> str:
    refs = "".join(f'<sup class="r">[{n}]</sup>' for n in p.sources)
    return html.escape(p.text) + refs


def render(brief: Brief, lang: str = "en", model: str = "", tz: str = "Europe/Rome") -> str:
    ov, weekly = brief.overlay, brief.edition == "weekly"
    by_label = {ln.spec.label: ln for ln in brief.lines}
    tiles = []
    for label in TILES:
        ln = by_label.get(label)
        if ln and ln.last:
            tiles.append({"label": label.replace(" yield", ""), "value": figures.fmt_value(ln).replace(" USD", " $"),
                          "change": pct(ln.change(7 if weekly else 1), ln.spec.rate) + (" week" if weekly else "")})
    groups = []
    for g in ("Equities", "Rates", "FX & commodities"):
        rows = [{"label": ln.spec.label, "last": figures.fmt_value(ln) if ln.last else "–",
                 "d": pct(ln.change(1), ln.spec.rate), "w": pct(ln.change(7), ln.spec.rate),
                 "m": pct(ln.change(30), ln.spec.rate), "spark": sparkline(ln.rows) if ln.spec.cadence == "daily" else "",
                 "source": SOURCE_NAMES.get(ln.source, ln.source) + (f" · {ln.spec.note}" if ln.spec.note else "")}
                for ln in brief.lines if ln.spec.group == g]
        groups.append({"name": g, "rows": rows})
    last_days = [ln.last[0] for ln in brief.lines if ln.last and ln.spec.cadence == "daily"]
    ctx = {"title": TITLES.get(lang, TITLES["en"])[brief.edition],
           "subtitle": f"{brief.day:%A %d %B %Y} · made {brief.created:%H:%M}",
           "footer": f"Franziska · {TITLES['en'][brief.edition]} · {brief.day:%d %b %Y} · not investment advice",
           "written": brief.written, "degraded": brief.degraded, "weekly": weekly, "tiles": tiles,
           "groups": groups,
           "fallback_title": "Markets and your portfolio",
           "closes_to": f"{max(last_days):%a %d %b}" if last_days else "–",
           "notes_markets": "Italy's 10-year yield is the ECB's monthly average; the euro area AAA yield is the "
                            "ECB's curve, close to the Bund."}
    if ov:
        d_eur, d_pct = ov.day_change
        p_eur, p_pct = ov.period_change
        ctx.update(ov=True, ov_total=money(ov.total_cents), ov_day=f"{pct(d_pct)} day", ov_period=pct(p_pct),
                   ov_cash=money(ov.cash_cents), ov_cash_w=f"{ov.cash_cents / ov.total_cents:.1%}" if ov.total_cents else "",
                   ov_day_pct=pct(d_pct), ov_day_eur=money(d_eur, True), ov_period_pct=pct(p_pct),
                   ov_period_eur=money(p_eur, True), ov_facts=" ".join(ov.facts()), ov_notes=ov.notes,
                   chart=line_chart(ov.series[-(23 if not weekly else 30):]),
                   chart_days=len(ov.series[-(23 if not weekly else 30):]),
                   ov_rows=[{"name": short(m.name), "value": money(m.value_cents), "weight": f"{m.weight:.1%}",
                             "day": pct(m.day_pct), "day_eur": money(m.day_cents, True), "period": pct(m.period_pct),
                             "period_eur": money(m.period_cents, True), "month": pct(m.month_pct), "priced": m.priced}
                            for m in ov.moves])
    if brief.written:
        w = brief.written
        order = {t: i for i, t in enumerate(SECTIONS)}
        ctx.update(sections=[{"title": s.title[0].upper() + s.title[1:], "points": [_point(p) for p in s.points]}
                             for s in sorted(w.sections, key=lambda s: order.get(s.title, 99)) if s.points],
                   holdings=[_point(p) for p in w.holdings], watch=[_point(p) for p in w.watch])
        ctx["sources"] = [_source(s, tz) for s in brief.stories]
    ctx["headlines"] = [{**_source(s, tz), "summary": s.summary[:260]} for s in brief.headlines]
    ctx["calendar"] = [{"day": f"{d:%a %d %b}", "title": t} for d, _, t, _ in brief.calendar if d >= brief.day]
    feeds = "CNBC, MarketWatch, FT, Il Sole 24 Ore, ANSA, ECB, Federal Reserve, Yahoo Finance, Google News"
    ctx["colophon"] = (f"Figures: Yahoo Finance, ECB Data Portal, FRED; calendars: federalreserve.gov, ecb.europa.eu, "
                       f"Nasdaq (earnings). News: {feeds}. "
                       + (f"Written by {model} from public data only; " if brief.written else "")
                       + "your portfolio figures are computed on this computer and never reach a model. "
                         "Not investment advice.")
    return Environment(autoescape=True).from_string(TEMPLATE).render(**ctx)


def _source(s, tz: str) -> dict:
    url = s.url
    shown = "news.google.com (redirect to the article)" if "news.google.com" in url else url.split("//", 1)[-1].split("?")[0]
    local = s.published.replace(tzinfo=UTC).astimezone(ZoneInfo(tz))
    return {"title": s.title, "source": s.source, "when": f"{local:%a %d %b %H:%M}", "url": url,
            "short_url": shown[:70] + ("…" if len(shown) > 70 else ""), "also": ", ".join(s.also[:3])}


def write_pdf(brief: Brief, out_dir: Path, lang: str = "en", model: str = "", tz: str = "Europe/Rome") -> Path:
    from weasyprint import HTML  # slow to import (fonts); only a report needs it

    out_dir.mkdir(parents=True, exist_ok=True)
    # Every run kept: a rerun the same day is a new file, not an overwrite
    path = out_dir / f"market-{brief.day:%Y-%m-%d}-{brief.edition}-{brief.created:%H%M%S}.pdf"
    HTML(string=render(brief, lang, model, tz)).write_pdf(path)
    return path


def caption(brief: Brief) -> str:
    """The Telegram message that goes with the PDF (a caption holds 1024 characters)."""
    weekly = brief.edition == "weekly"
    out = [f"📈 {'Weekly market report' if weekly else 'Markets'}, {brief.day:%a %d %b}"]
    if brief.written and brief.written.headline:
        out.append(brief.written.headline)
    if ov := brief.overlay:
        d_pct, p_pct = ov.day_change[1], ov.period_change[1]
        line = f"Portfolio {money(ov.total_cents)}"
        if d_pct is not None:
            line += f" ({d_pct:+.1f}% day"
            line += f", {p_pct:+.1f}% week)" if weekly and p_pct is not None else ")"
        if ov.total_cents:
            line += f" · cash {ov.cash_cents / ov.total_cents:.0%}"
        out.append(line)
        moved = [(m.period_cents, m.period_pct) if weekly else (m.day_cents, m.day_pct) for m in ov.moves]
        movers = sorted(zip(ov.moves, moved, strict=True), key=lambda x: abs(x[1][0]), reverse=True)[:2]
        out += [f"{'↑' if c > 0 else '↓'} {short(m.name)} {p or 0:+.1f}% ({money(c, True)})"
                for m, (c, p) in movers if c]
    by_label = {ln.spec.label: ln for ln in brief.lines}
    marks = []
    for label in ("S&P 500", "STOXX Europe 600", "FTSE MIB"):
        if (ln := by_label.get(label)) and (c := ln.change(7 if weekly else 1)) is not None:
            marks.append(f"{label.replace('STOXX Europe 600', 'Stoxx 600')} {c:+.1f}%")
    if (ln := by_label.get("EUR/USD")) and ln.last:
        marks.append(f"EUR/USD {ln.last[1]:.4f}")
    if marks:
        out.append(" · ".join(marks))
    if soon := [f"{d:%a %d}: {t}" for d, _, t, _ in brief.calendar if brief.day <= d][:2]:
        out.append("📅 " + " · ".join(soon))
    if brief.degraded:
        out.append("⚠️ " + brief.degraded[0])
    return "\n".join(out)[:1024]
