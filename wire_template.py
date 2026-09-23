#!/usr/bin/env python3
"""
"The Wire" email template - the newsletter's HTML, built to survive real mail clients.

Matches reference/the-wire-mock.html (Dispatch styling) but rebuilt for email: tables rather than
flexbox, every style inline, a fixed 620px centre column, web-safe fallbacks for Archivo (Helvetica/
Arial) and Spectral (Georgia), and colours that stay legible where a client forces dark mode.

Sections, in order: masthead, dateline, The Lead, The Feed, By the Numbers (our own data),
On the Move, Open Procurements, footer. Every section is skipped when it has nothing to show.
{unsub} in the output is replaced with each recipient's own link at send time.
"""
INK = "#17140F"
PAPER = "#F7F4ED"
OUTER = "#E7E1D4"
RED = "#C0341F"
FLAME = "#EE6A54"
MUTED = "#6A6458"
BODY = "#3A352C"
LINE = "#D8D2C4"
SOFT = "#E7E1D4"
SANS = "'Archivo',Helvetica,Arial,sans-serif"
SERIF = "'Spectral',Georgia,'Times New Roman',serif"
WIDTH = 620


def esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _row(inner, pad="0 34px", bg=PAPER, extra=""):
    """One full-width row of the centre column."""
    return ('<tr><td style="padding:' + pad + ';background:' + bg + ';' + extra + '">' + inner + "</td></tr>")


def masthead(date_label, issue_no):
    return ('<tr><td align="center" style="background:' + INK + ';padding:18px 34px 16px">'
            '<div style="font-family:' + SANS + ';font-weight:900;font-size:34px;letter-spacing:-1.5px;'
            'color:' + PAPER + ';line-height:1">TRANSIT<span style="color:' + FLAME + '">411</span></div>'
            '<div style="font-family:' + SANS + ';font-weight:800;font-size:19px;letter-spacing:.5px;'
            'text-transform:uppercase;color:' + PAPER + ';margin-top:10px;padding-top:10px;'
            'border-top:1px solid #3A352C;display:inline-block">The Wire</div>'
            '<div style="font-family:' + SANS + ';font-size:10px;letter-spacing:2.5px;text-transform:uppercase;'
            'color:#A69F90;margin-top:8px">Transit news, data &amp; moves</div></td></tr>'
            '<tr><td style="padding:12px 34px;background:' + PAPER + ';border-bottom:3px solid ' + INK + '">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
            '<td style="font-family:' + SANS + ';font-size:11px;letter-spacing:1.5px;text-transform:uppercase;'
            'color:' + MUTED + '">' + esc(date_label) + '</td>'
            '<td align="right" style="font-family:' + SANS + ';font-size:11px;letter-spacing:1.5px;'
            'text-transform:uppercase;color:' + MUTED + '">Issue No. ' + esc(issue_no) + "</td></tr></table></td></tr>")


def section_header(label, red=False):
    return _row('<div style="font-family:' + SANS + ';font-size:12px;font-weight:800;letter-spacing:1.5px;'
                'text-transform:uppercase;color:' + (RED if red else INK) + '">' + esc(label) + "</div>",
                pad="16px 34px 2px")


def lead(post):
    """The top story: kicker, headline and the take, with a thumbnail beside them rather than a
    banner across the column. Built as a two-cell table so it survives every mail client; without a
    picture it is simply one column."""
    if not post:
        return ""
    kicker = ('<div style="font-family:' + SANS + ';font-size:11px;font-weight:800;letter-spacing:1px;'
              'text-transform:uppercase;color:' + RED + '">Lead &mdash; ' + esc(post.get("pillar") or "News") + "</div>")
    headline = ('<h1 style="font-family:' + SANS + ';font-size:24px;line-height:1.14;letter-spacing:-.5px;'
                'margin:8px 0 10px;font-weight:800;color:' + INK + '">'
                '<a href="' + esc(post["url"]) + '" style="color:' + INK + ';text-decoration:none">'
                + esc(post["title"]) + "</a></h1>")
    take = ('<p style="font-family:' + SERIF + ';font-size:16px;line-height:1.5;color:' + BODY + ';margin:0">'
            + esc(post.get("summary") or "") + "</p>")
    more = ('<div style="font-family:' + SANS + ';font-size:12px;font-weight:700;margin-top:12px">'
            '<a href="' + esc(post["url"]) + '" style="color:' + RED + ';text-decoration:none">'
            'Read the full story &rarr;</a></div>')
    text_cell = kicker + headline + take + more
    if not post.get("image_url"):
        return _row(text_cell, pad="18px 34px", extra="border-bottom:1px solid " + LINE)
    # Alt text is styled too: plenty of clients block images by default, and an unstyled alt renders
    # as blue underlined link text in an empty box. This way a blocked image still reads as a caption.
    thumb = ('<a href="' + esc(post["url"]) + '" style="text-decoration:none;color:' + MUTED + '">'
             '<img src="' + esc(post["image_url"]) + '" width="200" alt="'
             + esc(post.get("image_alt") or post["title"]) + '" style="width:200px;max-width:200px;height:auto;'
             'display:block;border:1px solid #D2CBBB;background:' + OUTER + ';font-family:' + SANS + ';'
             'font-size:11px;line-height:1.4;color:' + MUTED + ';text-decoration:none"></a>')
    return ('<tr><td style="padding:18px 34px;background:' + PAPER + ';border-bottom:1px solid ' + LINE + '">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
            '<td valign="top" style="padding-right:16px">' + text_cell + "</td>"
            '<td valign="middle" width="200" style="width:200px">' + thumb + "</td>"
            "</tr></table></td></tr>")


def feed(posts):
    """The Feed: a few items, each with its pillar tag, a one-line why-it-matters and its source."""
    if not posts:
        return ""
    out = [section_header("The Feed", red=True)]
    for p in posts:
        src = ""
        if p.get("source"):
            src = ('<div style="font-family:' + SANS + ';font-size:12px;color:' + MUTED + ';margin-top:6px">'
                   + esc(p["source"]) + ' &middot; <a href="' + esc(p["url"]) + '" style="color:' + RED
                   + ';text-decoration:none">source</a></div>')
        out.append(_row('<div style="font-family:' + SANS + ';font-size:10px;font-weight:800;letter-spacing:1px;'
                        'text-transform:uppercase;color:' + RED + ';margin-bottom:5px">'
                        + esc(p.get("pillar") or "News") + "</div>"
                        '<h3 style="font-family:' + SANS + ';font-size:17px;font-weight:700;line-height:1.25;'
                        'margin:0 0 4px"><a href="' + esc(p["url"]) + '" style="color:' + INK
                        + ';text-decoration:none">' + esc(p["title"]) + "</a></h3>"
                        '<p style="font-family:' + SERIF + ';font-size:14px;line-height:1.45;color:#4A443A;margin:0">'
                        + esc(p.get("summary") or "") + "</p>" + src,
                        pad="13px 34px", extra="border-bottom:1px solid " + SOFT))
    return "".join(out)


def numbers(stat):
    """The dark data block - one live figure from our own pipeline, with a link to ask it yourself."""
    if not stat:
        return ""
    cta = ""
    if stat.get("cta_url"):
        cta = ('<a href="' + esc(stat["cta_url"]) + '" style="display:inline-block;font-family:' + SANS + ';'
               'font-size:12px;font-weight:800;letter-spacing:.5px;text-transform:uppercase;color:' + INK + ';'
               'background:' + FLAME + ';padding:10px 18px;text-decoration:none">'
               + esc(stat.get("cta") or "Ask it yourself") + " &rarr;</a>")
    return ('<tr><td style="padding:16px 34px;background:' + PAPER + '">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="background:' + INK + '"><tr><td align="center" style="padding:18px 24px">'
            '<div style="font-family:' + SANS + ';font-size:11px;font-weight:800;letter-spacing:1.5px;'
            'text-transform:uppercase;color:' + FLAME + '">By the Numbers &middot; from our data</div>'
            '<div style="font-family:' + SANS + ';font-size:40px;font-weight:900;letter-spacing:-1.5px;'
            'margin:4px 0;color:' + PAPER + '">' + esc(stat["figure"]) + "</div>"
            '<p style="font-family:' + SERIF + ';font-size:14px;line-height:1.5;color:#C9C2B4;margin:0 0 14px">'
            + esc(stat["caption"]) + "</p>" + cta + "</td></tr></table></td></tr>")


def moves(items):
    """On the Move: people items, one line each."""
    if not items:
        return ""
    out = [section_header("On the Move")]
    for p in items:
        out.append(_row('<div style="font-family:' + SERIF + ';font-size:15px;line-height:1.5;color:' + INK + '">'
                        '<a href="' + esc(p["url"]) + '" style="color:' + INK + ';text-decoration:none">'
                        '<b style="font-family:' + SANS + ';font-weight:700">' + esc(p["title"]) + "</b></a>"
                        + ('<span style="color:' + MUTED + '"> &mdash; ' + esc(p["summary"]) + "</span>"
                           if p.get("summary") else "") + "</div>",
                        pad="12px 34px", extra="border-bottom:1px solid " + SOFT))
    return "".join(out)


def procurements(items):
    """Open Procurements: a compact list, included only when there is something worth listing."""
    if not items:
        return ""
    out = [section_header("Open Procurements")]
    for p in items:
        where = " &middot; ".join(filter(None, [p.get("source"), p.get("state")]))
        out.append(_row('<div style="font-family:' + SANS + ';font-size:14px;color:' + INK + '">'
                        '<a href="' + esc(p["url"]) + '" style="color:' + INK + ';text-decoration:none">'
                        '<b style="font-weight:700">' + esc(p["title"]) + "</b></a>"
                        + ('<span style="color:' + MUTED + ';font-size:12px"> &middot; ' + esc(where) + "</span>"
                           if where else "") + "</div>",
                        pad="12px 34px", extra="border-bottom:1px solid " + SOFT))
    return "".join(out)


def footer(site):
    sub = site + "/#newsletter"
    return ('<tr><td align="center" style="background:' + INK + ';color:#A69F90;padding:28px 34px;'
            'font-family:' + SANS + ';font-size:12px;line-height:1.7">'
            '<div style="font-weight:900;font-size:20px;letter-spacing:-1px;color:' + PAPER + ';margin-bottom:10px">'
            'TRANSIT<span style="color:' + FLAME + '">411</span></div>'
            'Forward this to a colleague &middot; <a href="' + esc(sub) + '" style="color:' + FLAME + ';'
            'text-decoration:none">They can subscribe here</a><br>'
            "You're receiving this because you subscribed at transit411.net.<br>"
            '<a href="{unsub}" style="color:' + FLAME + ';text-decoration:none">Unsubscribe</a><br>'
            '<span style="color:' + MUTED + '">Transit411 is a product of Shepherd Labs, LLC.</span>'
            "</td></tr>")


def render_html(data):
    """The full issue. `data` carries date_label, issue_no, lead, feed[], stat, moves[], procurements[], site."""
    preheader = data.get("preheader") or ""
    body = (masthead(data["date_label"], data["issue_no"])
            + lead(data.get("lead"))
            + feed(data.get("feed") or [])
            + numbers(data.get("stat"))
            + moves(data.get("moves") or [])
            + procurements(data.get("procurements") or [])
            + footer(data.get("site") or "https://transit411.net"))
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="light only"><meta name="supported-color-schemes" content="light only">'
            "<title>The Wire &mdash; Transit411</title></head>"
            '<body style="margin:0;padding:0;background:' + OUTER + ';font-family:' + SERIF + ';color:' + INK + '">'
            '<div style="display:none;font-size:1px;color:' + OUTER + ';max-height:0;overflow:hidden">'
            + esc(preheader) + "</div>"
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="background:' + OUTER + '"><tr><td align="center" style="padding:0">'
            '<table role="presentation" width="' + str(WIDTH) + '" cellpadding="0" cellspacing="0" border="0" '
            'style="width:' + str(WIDTH) + 'px;max-width:100%;background:' + PAPER + '">'
            + body + "</table></td></tr></table></body></html>")


def render_text(data):
    """The plain-text alternative - same running order, no markup."""
    out = ["TRANSIT411 - THE WIRE", data["date_label"] + "  |  Issue No. " + str(data["issue_no"]), ""]
    if data.get("lead"):
        p = data["lead"]
        out += ["THE LEAD - " + (p.get("pillar") or "News"), p["title"], (p.get("summary") or "").strip(), p["url"], ""]
    if data.get("feed"):
        out.append("THE FEED")
        for p in data["feed"]:
            out += ["* [" + (p.get("pillar") or "News") + "] " + p["title"], "  " + (p.get("summary") or "").strip(),
                    "  " + p["url"]]
        out.append("")
    if data.get("stat"):
        out += ["BY THE NUMBERS", data["stat"]["figure"] + " - " + data["stat"]["caption"]]
        if data["stat"].get("cta_url"):
            out.append(data["stat"]["cta_url"])
        out.append("")
    for label, key in (("ON THE MOVE", "moves"), ("OPEN PROCUREMENTS", "procurements")):
        if data.get(key):
            out.append(label)
            for p in data[key]:
                out += ["* " + p["title"] + (" - " + p["summary"] if p.get("summary") else ""), "  " + p["url"]]
            out.append("")
    out += ["--", "Forward this to a colleague: " + (data.get("site") or "") + "/#newsletter",
            "You're receiving this because you subscribed at transit411.net.",
            "Unsubscribe: {unsub}", "Transit411 is a product of Shepherd Labs, LLC."]
    return "\n".join(out)
