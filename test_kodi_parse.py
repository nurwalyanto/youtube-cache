#!/usr/bin/env python3
"""Simulate Kodi's HTTPDirectory.cpp parsing logic."""
import re
import sys
import urllib.request

# === Kodi regexes (from HTTPDirectory.cpp) ===

# Item: <a href="LINK" ...> NAME </a> ...metadata...
re_item = re.compile(
    r'<a href="([^"]*)"[^>]*>\s*(.*?)\s*</a>(.+?)(?=<a|</tr|$)',
    re.IGNORECASE | re.DOTALL
)

# Kodi uses CRegExp(true) = case-insensitive for all date/size regexes
RE_CI = re.IGNORECASE

re_date_html = re.compile(
    r'<td align="right">([0-9]{2})-([A-Z]{3})-([0-9]{4}) ([0-9]{2}):([0-9]{2}) +</td>',
    RE_CI
)
re_date_nginx_fancy = re.compile(
    r'<td class="date">([0-9]{4})-([A-Z]{3})-([0-9]{2}) ([0-9]{2}):([0-9]{2})</td>',
    RE_CI
)
re_date_nginx = re.compile(
    r'([0-9]{2})-([A-Z]{3})-([0-9]{4}) ([0-9]{2}):([0-9]{2})',
    RE_CI
)
re_date_lighttp = re.compile(
    r'<td class="m">([0-9]{4})-([A-Z]{3})-([0-9]{2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})</td>',
    RE_CI
)
re_date_apache_new = re.compile(
    r'<td align="right">([0-9]{4})-([0-9]{2})-([0-9]{2}) ([0-9]{2}):([0-9]{2}) +</td>'
)
re_date_generic = re.compile(
    r'([0-9]{4})-([0-9]{2})-([0-9]{2}) ([0-9]{2}):([0-9]{2})'
)

re_size_html = re.compile(r'> *([0-9.]+) *(B|K|M|G| )(iB)?</td>')
re_size = re.compile(r' +([0-9]+)([BKMG])?(?=\s|<|$)')


def parse_kodi_directory(html):
    """Returns list of (accepted: bool, name: str, link: str, reason: str)."""
    results = []
    offset = 0
    while offset < len(html):
        m = re_item.search(html, offset)
        if not m:
            break
        offset = m.end()
        link = m.group(1)
        name = m.group(2).strip()
        metadata = m.group(3).strip()

        # Strip leading slash
        if link.startswith('/'):
            link = link[1:]

        # Decode and clean
        from urllib.parse import unquote
        link_temp = unquote(link)
        if link_temp.endswith('/'):
            link_temp = link_temp[:-1]

        name_temp = name
        if name_temp.endswith('/'):
            name_temp = name_temp[:-1]

        # Skip parent
        if link_temp == '..' or not link_temp:
            results.append((False, name, link, 'skipped (.. or empty)'))
            continue

        # NameMatchesLink check
        def name_matches_link(n, l):
            if n == l:
                return True
            if ':' in n and './' + n == l:
                return True
            return False

        if not name_matches_link(name_temp, link_temp):
            results.append((False, name, link,
                            f'REJECTED: name="{name_temp}" != link="{link_temp}"'))
            continue

        # Date parsing
        day = month = year = hour = minute = ''
        month_num = 0
        d = ''

        if re_date_html.search(metadata):
            g = re_date_html.search(metadata)
            day, month, year, hour, minute = g.group(1), g.group(2), g.group(3), g.group(4), g.group(5)
            d = f'{day}-{month}-{year} {hour}:{minute} (html)'
        elif re_date_nginx_fancy.search(metadata):
            g = re_date_nginx_fancy.search(metadata)
            day, month, year, hour, minute = g.group(3), g.group(2), g.group(1), g.group(4), g.group(5)
            d = f'{day}-{month}-{year} {hour}:{minute} (nginx fancy)'
        elif re_date_nginx.search(metadata):
            g = re_date_nginx.search(metadata)
            day, month, year, hour, minute = g.group(1), g.group(2), g.group(3), g.group(4), g.group(5)
            d = f'{day}-{month}-{year} {hour}:{minute} (nginx)'
        elif re_date_lighttp.search(metadata):
            g = re_date_lighttp.search(metadata)
            day, month, year, hour, minute = g.group(3), g.group(2), g.group(1), g.group(4), g.group(5)
            d = f'{day}-{month}-{year} {hour}:{minute} (lighttp)'
        elif re_date_apache_new.search(metadata):
            g = re_date_apache_new.search(metadata)
            day, month, year, hour, minute = g.group(3), g.group(2), g.group(1), g.group(4), g.group(5)
            d = f'{year}-{month}-{day} {hour}:{minute} (apache new)'
        elif re_date_generic.search(metadata):
            g = re_date_generic.search(metadata)
            day, month, year, hour, minute = g.group(3), g.group(2), g.group(1), g.group(4), g.group(5)
            d = f'{year}-{month}-{day} {hour}:{minute} (generic)'

        # Size parsing
        sz = ''
        if re_size_html.search(metadata):
            g = re_size_html.search(metadata)
            sz = g.group(1) + g.group(2)
        elif re_size.search(metadata):
            g = re_size.search(metadata)
            sz = g.group(1) + (g.group(2) or '')

        results.append((True, name, link, f'ACCEPTED | date={d} | size={sz}'))

    return results


if __name__ == '__main__':
    urls = sys.argv[1:] or [
        'http://localhost:5000/browse/',
        'http://localhost:5000/browse/fSGpAQPtGoc/',
    ]
    for url in urls:
        print(f'\n=== {url} ===')
        try:
            html = urllib.request.urlopen(url, timeout=10).read().decode()
        except Exception as e:
            print(f'  FETCH ERROR: {e}')
            continue
        results = parse_kodi_directory(html)
        for ok, name, link, msg in results:
            print(f'  {msg}')
        if not results:
            print('  (no items found)')
