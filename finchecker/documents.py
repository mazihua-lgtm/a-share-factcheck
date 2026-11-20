"""Federate public announcement references and explicitly supplied research material."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests

from finchecker.market import SHANGHAI, MarketUnavailable, normalize_symbol

KINDS = ('announcement', 'report', 'news', 'minutes', 'dongmi')
CNINFO = 'https://www.cninfo.com.cn/new/hisAnnouncement/query'
PUBLIC_LINKS = 'https://static.cninfo.com.cn/'


def local_documents(root):
    path = root / 'documents.jsonl' if root is not None else None
    if path is None or not path.is_file():
        raise MarketUnavailable('未配置 documents.jsonl；非公告类别仅搜索你有权使用的本地目录')
    items = {}
    try:
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                if not line.strip():
                    continue
                entry = json.loads(line)
                ref = entry['ref']
                if not isinstance(ref, str) or not ref.startswith('local:') or ref in items:
                    raise ValueError('invalid or duplicate ref')
                day = date.fromisoformat(entry['date']).isoformat()
                target = urlsplit(entry['url'])
                if entry['kind'] not in KINDS or target.scheme not in {'https', 'http'} or not target.hostname:
                    raise ValueError('invalid kind or URL')
                title = entry['title']
                summary = entry.get('summary')
                if not isinstance(title, str) or not title.strip() or (summary is not None and not isinstance(summary, str)):
                    raise ValueError('invalid document text')
                items[ref] = {'ref': ref, 'kind': entry['kind'], 'title': title, 'date': day,
                              'url': entry['url'], 'summary': summary,
                              'symbol': normalize_symbol(entry['symbol']) if entry.get('symbol') else None,
                              'source': 'local_documents'}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise MarketUnavailable('documents.jsonl 格式无效') from error
    return items


def public_announcements(keyword, start, end, symbol=None):
    query = {'pageNum': 1, 'pageSize': 30, 'column': 'sse' if symbol and symbol.startswith('6') else 'szse',
             'tabName': 'fulltext', 'searchkey': keyword, 'seDate': f'{start}~{end}'}
    if symbol and not symbol.startswith(('6', '00', '30')):
        raise MarketUnavailable('公开公告查询当前仅核验沪深接口；其他市场请导入公告索引')
    if not symbol:
        raise ValueError('公开公告检索需要沪深股票代码')
    query['searchkey'] = symbol
    try:
        response = requests.post(CNINFO, data=query, headers={'Referer': 'https://www.cninfo.com.cn/'},
                                 timeout=10, allow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        if 'announcements' not in payload:
            raise ValueError('unexpected response')
        output = []
        for entry in payload['announcements'] or []:
            if entry.get('secCode') != symbol or keyword.casefold() not in entry['announcementTitle'].casefold():
                continue
            link = entry['adjunctUrl']
            if not isinstance(link, str) or not re.fullmatch(r'finalpage/\d{4}-\d{2}-\d{2}/\d+\.[Pp][Dd][Ff]', link):
                continue
            published = datetime.fromtimestamp(entry['announcementTime'] / 1000, timezone.utc).astimezone(SHANGHAI).date().isoformat()
            if not start <= published <= end:
                continue
            output.append({'ref': 'cninfo:' + link, 'kind': 'announcement', 'date': published,
                           'title': entry['announcementTitle'], 'symbol': symbol,
                           'url': PUBLIC_LINKS + link, 'source': 'cninfo_index'})
        return output, bool(payload.get('hasMore')) or (payload.get('totalAnnouncement') or 0) > 30
    except (requests.RequestException, KeyError, TypeError, ValueError, OverflowError) as error:
        raise MarketUnavailable('巨潮公告索引暂不可用或返回结构有变') from error


def search(root, keyword, kind, symbol=None, start_date=None, end_date=None, limit=20):
    end = date.fromisoformat(end_date) if end_date else datetime.now(SHANGHAI).date()
    start = date.fromisoformat(start_date) if start_date else end - timedelta(days=29)
    if not 0 <= (end - start).days < 90:
        raise ValueError('日期范围须为 1 至 90 个自然日（含首尾）')
    symbol = normalize_symbol(symbol) if symbol else None
    if kind == 'announcement' and symbol:
        items, incomplete = public_announcements(keyword, start.isoformat(), end.isoformat(), symbol)
        source = 'cninfo_index'
    else:
        entries = local_documents(root)
        needle = keyword.casefold()
        items = [{key: value for key, value in entry.items() if key != 'summary'}
                 for entry in entries.values()
                 if entry['kind'] == kind and start.isoformat() <= entry['date'] <= end.isoformat()
                 and (not symbol or entry['symbol'] == symbol)
                 and needle in (entry['title'] + ' ' + (entry['summary'] or '')).casefold()]
        incomplete = False
        source = 'local_documents'
    items.sort(key=lambda entry: (entry['date'], entry['ref']), reverse=True)
    return {'status': 'ok', 'source': source, 'start_date': start.isoformat(), 'end_date': end.isoformat(),
            'truncated': incomplete or len(items) > limit, 'rows': items[:limit]}


def get(root, ref):
    if ref.startswith('cninfo:'):
        link = ref.removeprefix('cninfo:')
        if not re.fullmatch(r'finalpage/\d{4}-\d{2}-\d{2}/\d+\.[Pp][Dd][Ff]', link):
            raise ValueError('无效的巨潮公告引用')
        return {'status': 'source_only', 'ref': ref, 'source': 'cninfo_index',
                'url': PUBLIC_LINKS + link, 'summary': None, 'basis': '链接定位，不代表已读取或核验正文'}
    entry = local_documents(root).get(ref)
    return {'status': 'ok', **entry} if entry else {'status': 'not_found', 'ref': ref}
