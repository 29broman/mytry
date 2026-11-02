import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EPG_URL = 'https://www.open-epg.com/generate/yCBAAdapZF.xml'
INPUT_XML = os.path.join(BASE_DIR, 'yCBAAdapZF.xml')
CACHE_FILE = os.path.join(BASE_DIR, 'translations.json')
GITHUB_CONFIG_FILE = os.path.join(BASE_DIR, 'github_config.json')
OUTPUT_ALL_XML = os.path.join(BASE_DIR, 'yCBAAdapZF_translated_all.xml')
OUTPUT_FILTERED_XML = os.path.join(BASE_DIR, 'yCBAAdapZF_translated_27channels.xml')
OUTPUT_MD = os.path.join(BASE_DIR, 'schedule_en.md')

TARGET_CHANNELS = [
    'Arena PREMIUM 1.rs', 'Arena PREMIUM 3.rs', 'Arena Premium 1.rs', 'Arena Premium 2.rs', 'Arena Premium 3.rs',
    'ZiggoSport.nl', 'ZiggoSport2.nl', 'ZiggoSport3.nl', 'ZiggoSport4.nl', 'ZiggoSport5.nl', 'ZiggoSport6.nl',
    'Arenasport1.hr', 'Arena3Premium.ba', 'CanalPlusFoot.fr', 'CanalPlusLive3.fr', 'CanalPlusPremierLeague.fr',
    'CanalPlusSport.fr', 'CanalPlusSport360.fr', 'DigiSport1.ro', 'DigiSport2.ro', 'DigiSport3.ro', 'DigiSport4.ro',
    'NovasportsPremierLeague.gr', 'SkySportAustria1.at', 'SkySportPremierLeague.de', 'Sky Sport Calcio.it', 'Sky Sport MotoGP.it'
]
TARGET_SET = set(TARGET_CHANNELS)

SUFFIX_TO_LANG = {
    '.rs': 'sr',
    '.ba': 'bs',
    '.hr': 'hr',
    '.nl': 'nl',
    '.fr': 'fr',
    '.ro': 'ro',
    '.gr': 'el',
    '.at': 'de',
    '.de': 'de',
    '.it': 'it',
}

OVERRIDES = {
    'PL: 90in30: TOT - CRY, 9. Spieltag': 'PL: 90in30: TOT - CRY, Matchday 9',
    'Admiral BL': 'Admiral Bundesliga',
}

def get_lang_for_channel(ch: str) -> str:
    for ext, lang in SUFFIX_TO_LANG.items():
        if ch.endswith(ext):
            return lang
    return 'auto'

def download_epg():
    print(f'Downloading fresh EPG from {EPG_URL} ...')
    req = urllib.request.Request(EPG_URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    with open(INPUT_XML, 'wb') as f:
        f.write(data)
    print(f'Saved {len(data)} bytes to {INPUT_XML}')

def translate_text(text: str, sl: str = 'auto') -> str:
    url = (
        f'https://translate.googleapis.com/translate_a/single?client=gtx'
        f'&sl={sl}&tl=en&dt=t&q={urllib.parse.quote(text)}'
    )
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        res = json.loads(resp.read().decode('utf-8'))
        return ''.join(part[0] for part in res[0] if part and part[0])

def translate_batch(batch_items, sl: str):
    joined = '\n'.join(batch_items)
    try:
        translated_joined = translate_text(joined, sl=sl)
        parts = translated_joined.split('\n')
        if len(parts) == len(batch_items):
            return {orig: trans.strip() for orig, trans in zip(batch_items, parts)}
    except Exception:
        pass
    result = {}
    for item in batch_items:
        try:
            result[item] = translate_text(item, sl=sl).strip()
        except Exception as e:
            print(f'Error translating {item[:30]}: {e}')
            result[item] = item
    return result

def github_put_file(repo: str, token: str, repo_path: str, local_path: str, branch: str = 'main'):
    api_url = f'https://api.github.com/repos/{repo}/contents/{repo_path}'
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'EPG-Translator-Bot',
        'X-GitHub-Api-Version': '2022-11-28',
    }
    sha = None
    try:
        req = urllib.request.Request(f'{api_url}?ref={branch}', headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            existing = json.loads(resp.read().decode('utf-8'))
            sha = existing.get('sha')
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

    with open(local_path, 'rb') as f:
        content_b64 = base64.b64encode(f.read()).decode('ascii')

    payload = {
        'message': f'chore(epg): update {repo_path}',
        'content': content_b64,
        'branch': branch,
    }
    if sha:
        payload['sha'] = sha

    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        api_url,
        data=body,
        headers={**headers, 'Content-Type': 'application/json'},
        method='PUT',
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        res = json.loads(resp.read().decode('utf-8'))
        print(f'Uploaded {repo_path} -> {res.get("content", {}).get("html_url", "OK")}')

def upload_to_github():
    if not os.path.exists(GITHUB_CONFIG_FILE):
        print(f'GitHub config not found at {GITHUB_CONFIG_FILE}. Skipping GitHub upload.')
        return
    with open(GITHUB_CONFIG_FILE, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    repo = cfg.get('repo')
    token = cfg.get('token')
    branch = cfg.get('branch', 'main')
    if not repo or not token or 'YOUR_' in repo or 'YOUR_' in token:
        print('Please set your actual "repo" and "token" in github_config.json to enable GitHub upload.')
        return

    files_to_sync = [
        ('yCBAAdapZF_translated_27channels.xml', OUTPUT_FILTERED_XML),
        ('yCBAAdapZF_translated_all.xml', OUTPUT_ALL_XML),
        ('schedule_en.md', OUTPUT_MD),
        ('translations.json', CACHE_FILE),
        ('translate_epg.py', os.path.abspath(__file__)),
        ('.github/workflows/update-epg.yml', os.path.join(BASE_DIR, '.github', 'workflows', 'update-epg.yml')),
    ]
    for repo_path, local_path in files_to_sync:
        if os.path.exists(local_path):
            try:
                github_put_file(repo, token, repo_path, local_path, branch=branch)
            except urllib.error.HTTPError as e:
                err_body = e.read().decode('utf-8', errors='replace')
                print(f'Failed to upload {repo_path} (HTTP {e.code}): {err_body}')
            except Exception as e:
                print(f'Failed to upload {repo_path}: {e}')

def main():
    parser = argparse.ArgumentParser(description='Translate Open-EPG XML for 27 channels into English.')
    parser.add_argument('--fetch', action='store_true', help='Download fresh XML from open-epg.com before translating')
    parser.add_argument('--upload-github', action='store_true', help='Upload translated files to GitHub using github_config.json')
    args = parser.parse_args()

    if args.fetch or not os.path.exists(INPUT_XML):
        download_epg()

    tree = ET.parse(INPUT_XML)
    root = tree.getroot()

    cache = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)
    cache.update(OVERRIDES)

    lang_groups = {}
    for p in root.findall('programme'):
        ch = p.get('channel')
        if ch in TARGET_SET:
            sl = get_lang_for_channel(ch)
            for tag in ('title', 'sub-title', 'desc'):
                for el in p.findall(tag):
                    if el.text and el.text.strip():
                        s = el.text.strip()
                        if s not in cache:
                            lang_groups.setdefault(sl, set()).add(s)

    batches = []
    for sl, items in lang_groups.items():
        items_list = sorted(list(items))
        current_batch = []
        current_len = 0
        for item in items_list:
            if len(current_batch) >= 12 or (current_len + len(item) > 1200 and current_batch):
                batches.append((sl, current_batch))
                current_batch = [item]
                current_len = len(item)
            else:
                current_batch.append(item)
                current_len += len(item) + 1
        if current_batch:
            batches.append((sl, current_batch))

    print(f'Total batches to translate: {len(batches)}')
    if batches:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(translate_batch, b, sl): (sl, b) for sl, b in batches}
            done_count = 0
            for fut in as_completed(futures):
                res = fut.result()
                cache.update(res)
                done_count += 1
                if done_count % 20 == 0 or done_count == len(batches):
                    print(f'Completed {done_count}/{len(batches)} batches')

        cache.update(OVERRIDES)
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

    translated_prog_count = 0
    for p in root.findall('programme'):
        ch = p.get('channel')
        if ch in TARGET_SET:
            translated_prog_count += 1
            for tag in ('title', 'sub-title', 'desc'):
                for el in p.findall(tag):
                    if el.text and el.text.strip():
                        s = el.text.strip()
                        el.text = cache.get(s, s)

    print(f'Translated {translated_prog_count} programmes across {len(TARGET_CHANNELS)} channels.')

    with open(OUTPUT_ALL_XML, 'wb') as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8" ?>\n')
        tree.write(f, encoding='utf-8', xml_declaration=False)

    filtered_root = ET.Element(root.tag, root.attrib)
    filtered_root.text = '\n'
    for ch_el in root.findall('channel'):
        if ch_el.get('id') in TARGET_SET:
            filtered_root.append(ch_el)
    for p_el in root.findall('programme'):
        if p_el.get('channel') in TARGET_SET:
            filtered_root.append(p_el)

    filtered_tree = ET.ElementTree(filtered_root)
    with open(OUTPUT_FILTERED_XML, 'wb') as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8" ?>\n')
        filtered_tree.write(f, encoding='utf-8', xml_declaration=False)

    def fmt_time(raw: str) -> str:
        try:
            dt = datetime.strptime(raw[:14], '%Y%m%d%H%M%S')
            return dt.strftime('%Y-%m-%d %H:%M UTC')
        except Exception:
            return raw

    progs_by_ch = {ch: [] for ch in TARGET_CHANNELS}
    for p_el in filtered_root.findall('programme'):
        ch = p_el.get('channel')
        if ch in progs_by_ch:
            start = fmt_time(p_el.get('start', ''))
            stop = fmt_time(p_el.get('stop', ''))
            title = (p_el.findtext('title') or '').strip()
            sub = (p_el.findtext('sub-title') or '').strip()
            desc = (p_el.findtext('desc') or '').strip()
            progs_by_ch[ch].append((start, stop, title, sub, desc))

    with open(OUTPUT_MD, 'w', encoding='utf-8') as f:
        f.write('# Translated EPG Schedule (27 Channels - English)\n\n')
        for ch in TARGET_CHANNELS:
            items = progs_by_ch[ch]
            f.write(f'## {ch} ({len(items)} programmes)\n\n')
            f.write('| Start (UTC) | Stop (UTC) | Title | Sub-title / Description |\n')
            f.write('|---|---|---|---|\n')
            for start, stop, title, sub, desc in items:
                extra = desc if desc and desc != title else ''
                if sub and sub != title and sub != desc:
                    extra = f'**{sub}** — {extra}' if extra else f'**{sub}**'
                title_esc = title.replace('|', '\\|')
                extra_esc = extra.replace('|', '\\|')
                f.write(f'| {start} | {stop} | {title_esc} | {extra_esc} |\n')
            f.write('\n')

    print('All output files written successfully.')

    if args.upload_github:
        upload_to_github()

if __name__ == '__main__':
    main()
