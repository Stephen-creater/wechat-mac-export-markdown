#!/usr/bin/env python3
"""微信 4.x 本地数据库 → Markdown 全量导出（只读）。

用法：
  SQLCIPHER_LIBRARY=... .venv/bin/python tools/export_md.py [--only 名称或username ...] [--out 目录]
"""
import argparse, hashlib, html, json, os, re, sys, time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import zstandard

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / 'scripts'))
from sqlcipher_probe import DB, load_key  # noqa: E402

XWECHAT = Path.home() / 'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files'


def find_account():
    """账号目录：优先用环境变量 WECHAT_ACCOUNT_DIR，否则取本机唯一一个带 db_storage 的账号目录。"""
    env = os.environ.get('WECHAT_ACCOUNT_DIR')
    if env:
        return Path(env).expanduser()
    found = sorted(d.parent for d in XWECHAT.glob('*/db_storage'))
    if len(found) != 1:
        sys.exit(f'找到 {len(found)} 个微信账号目录，请用环境变量 WECHAT_ACCOUNT_DIR 指定一个：{[str(d) for d in found]}')
    return found[0]


ACCOUNT = find_account()
ROOT = ACCOUNT / 'db_storage'
MSGDIR = ACCOUNT / 'msg'
KEYS = HERE / '.wechat-local/keys.json'
SELF = os.environ.get('WECHAT_SELF_WXID') or re.sub(r'_[0-9a-f]{4}$', '', ACCOUNT.name)  # 本人 wxid：目录名去掉末尾 4 位后缀
SPLIT_AT = 20000        # 超过这么多条按年拆文件
MONTH_SPLIT_AT = 40000  # 单年超过这么多条再按月拆

ZD = zstandard.ZstdDecompressor()
PREFIX_RE = re.compile(r'^([A-Za-z0-9_\-@.]{3,80}):\n')


def open_db(rel):
    p = ROOT / rel
    return DB(str(p), load_key(KEYS, str(p)))


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def ts(t, fmt='%Y-%m-%d %H:%M:%S'):
    return time.strftime(fmt, time.localtime(t))


def unpack(hexstr):
    raw = bytes.fromhex(hexstr or '')
    if raw[:4] == b'\x28\xb5\x2f\xfd':
        try:
            raw = ZD.decompressobj().decompress(raw)
        except Exception:
            return '[内容解压失败]'
    return raw.decode('utf-8', 'replace')


# ---------- protobuf 小工具 ----------
def pb_fields(b):
    i, n = 0, len(b)
    def varint():
        nonlocal i
        v = s = 0
        while i < n:
            c = b[i]; i += 1
            v |= (c & 0x7F) << s; s += 7
            if c < 0x80:
                return v
        raise ValueError
    try:
        while i < n:
            key = varint(); fn, wt = key >> 3, key & 7
            if wt == 0:
                yield fn, wt, varint()
            elif wt == 1:
                yield fn, wt, b[i:i+8]; i += 8
            elif wt == 2:
                ln = varint(); yield fn, wt, b[i:i+ln]; i += ln
            elif wt == 5:
                yield fn, wt, b[i:i+4]; i += 4
            else:
                return
    except (ValueError, IndexError):
        return


def voice_text(packed):
    for fn, wt, v in pb_fields(packed):
        if fn == 5 and wt == 2:
            for f2, w2, v2 in pb_fields(v):
                if f2 == 2 and w2 == 2:
                    try:
                        t = v2.decode('utf-8').strip()
                        if t:
                            return t
                    except UnicodeDecodeError:
                        pass
    return None


def room_members(buf):
    """chat_room.ext_buffer 里的群昵称：{username: 群名片}"""
    out = {}
    def walk(b, depth):
        if depth > 3:
            return
        for fn, wt, v in pb_fields(b):
            if wt != 2 or not v:
                continue
            sub = list(pb_fields(v))
            d = {f: x for f, w, x in sub if w == 2}
            u, nm = d.get(1), d.get(2)
            if u and nm:
                try:
                    u, nm = u.decode(), nm.decode()
                    if re.fullmatch(r'[A-Za-z0-9_\-@.]{3,80}', u):
                        out[u] = nm
                        continue
                except UnicodeDecodeError:
                    pass
            walk(v, depth + 1)
    walk(buf, 0)
    return out


# ---------- XML 工具 ----------
def xml_root(s):
    s = s[s.find('<'):] if '<' in s else s
    try:
        return ET.fromstring(s)
    except ET.ParseError:
        s2 = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', s)
        try:
            return ET.fromstring(s2)
        except ET.ParseError:
            return None


def tx(node, path):
    if node is None:
        return ''
    e = node.find(path)
    return (e.text or '').strip() if e is not None and e.text else ''


def rx(s, tag):
    m = re.search(rf'<{tag}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>', s, re.S)
    return html.unescape(m.group(1)).strip() if m else ''


def one_line(s, limit=160):
    s = re.sub(r'\s+', ' ', s or '').strip()
    return s if len(s) <= limit else s[:limit] + '…'


def human_size(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ''
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:.0f}{u}' if u == 'B' else f'{n:.1f}{u}'
        n /= 1024
    return f'{n:.1f}TB'


def strip_tags(s):
    s = re.sub(r'<br\s*/?>', '\n', s)
    s = re.sub(r'<[^>]+>', '', s)
    return html.unescape(s).strip()


def link(label, path):
    return f'[{label}](<{path}>)'


# ---------- 媒体索引 ----------
class Media:
    def __init__(self):
        self.video = {}
        vd = MSGDIR / 'video'
        if vd.exists():
            for p in vd.rglob('*.mp4'):
                self.video[p.stem] = p
        self.files = defaultdict(list)
        fd = MSGDIR / 'file'
        if fd.exists():
            for p in fd.rglob('*'):
                if p.is_file():
                    self.files[p.name].append(p)
        self._img_chat = None
        self._img = {}

    def image(self, chat, h):
        if self._img_chat != chat:
            self._img_chat, self._img = chat, {}
            d = MSGDIR / 'attach' / md5(chat)
            if d.exists():
                for p in d.rglob('*.dat'):
                    self._img[p.stem] = p
        return self._img.get(h)

    def file(self, name, size):
        cands = self.files.get(name, [])
        if size:
            exact = [p for p in cands if p.stat().st_size == int(size)]
            if exact:
                return exact[0]
        return cands[0] if len(cands) == 1 else None


# ---------- 消息渲染 ----------
def render_record(s):
    """转发的聊天记录 recorditem"""
    r = xml_root(s)
    if r is None:
        return []
    lines = []
    for it in r.iter('dataitem'):
        dt = it.get('datatype', '')
        who = tx(it, 'sourcename') or '?'
        when = tx(it, 'sourcetime')
        body = tx(it, 'datadesc')
        title = tx(it, 'datatitle')
        if dt == '2':
            body = '[图片]'
        elif dt == '3':
            body = '[语音]'
        elif dt in ('4', '15'):
            body = '[视频]'
        elif dt == '5':
            body = f'[链接] {title or body} {tx(it, "link") or tx(it, "weburlitem/link")}'.strip()
        elif dt == '8':
            body = f'[文件] {title}'
        elif dt == '17':
            body = f'[聊天记录] {title or body}'
        elif dt == '6':
            body = f'[位置] {title or body}'
        elif dt == '22' or dt == '23':
            body = f'[视频号] {title or body}'
        elif not body:
            body = title or f'[类型{dt}]'
        lines.append(f'- {who}{"（" + when + "）" if when else ""}：{one_line(body, 400)}')
    return lines


def render_app(s, sub, ctx):
    r = xml_root(s)
    am = r.find('appmsg') if r is not None else None
    if r is not None and r.tag == 'appmsg':
        am = r
    g = (lambda p: tx(am, p)) if am is not None else (lambda p: rx(s, p.split('/')[-1]))
    title, des, url = g('title'), g('des'), g('url')
    try:
        st = int(g('type') or sub)
    except ValueError:
        st = sub
    if st == 57:
        ref = am.find('refermsg') if am is not None else None
        who = tx(ref, 'displayname') if ref is not None else rx(s, 'displayname')
        rtype = tx(ref, 'type') if ref is not None else ''
        rc = tx(ref, 'content') if ref is not None else ''
        rc = PREFIX_RE.sub('', rc)
        if rtype == '49':
            rr = xml_root(rc)
            rc = '[分享] ' + (tx(rr, 'appmsg/title') or rx(rc, 'title'))
        elif rtype == '3':
            rc = '[图片]'
        elif rtype == '43':
            rc = '[视频]'
        elif rtype == '34':
            rc = '[语音]'
        elif rtype == '47':
            rc = '[表情]'
        elif rtype == '42':
            rc = '[名片]'
        elif rtype == '48':
            rc = '[位置]'
        elif rc.startswith('<'):
            rc = strip_tags(rc)
        return [f'{title}', '', f'↳ 引用 {who}：{one_line(rc, 200)}']
    if st in (6, 74):
        size = g('appattach/totallen')
        out = f'[文件] {title}' + (f'（{human_size(size)}）' if size else '')
        p = ctx['media'].file(title, size) if title else None
        if p:
            out += ' · ' + link('打开本地文件', p)
        return [out]
    if st == 19:
        lines = [f'[聊天记录] {title}']
        rec = g('recorditem')
        items = render_record(rec) if rec else []
        if items:
            lines += items
        elif des:
            lines += [one_line(x, 400) for x in des.split('\n') if x.strip()]
        return lines
    if st in (33, 36):
        app = g('sourcedisplayname') or g('weappinfo/nickname') or rx(s, 'sourcedisplayname')
        return [f'[小程序{("·" + app) if app else ""}] {title}'.strip()]
    if st == 51:
        nick = rx(s, 'nickname'); desc = rx(s, 'desc')
        return [f'[视频号] {nick}：{one_line(desc or title, 300)}']
    if st == 63:
        nick = rx(s, 'nickname'); desc = rx(s, 'desc')
        return [f'[视频号直播] {nick} {one_line(desc, 200)}'.strip()]
    if st == 50:
        return [f'[视频号名片] {rx(s, "nickname")}']
    if st == 62:
        return [f'[拍一拍] {title}']
    if st == 53:
        return ['[接龙]'] + [x for x in title.split('\n') if x.strip()]
    if st == 87:
        ann = strip_tags(rx(s, 'textannouncement') or g('textannouncement') or rx(s, 'announcement'))
        return ['[群公告]'] + ([x for x in ann.split('\n')] if ann else [])
    if st == 2000:
        fee = g('wcpayinfo/feedesc')
        memo = g('wcpayinfo/pay_memo')
        return [f'[转账] {fee} {des}'.strip() + (f'（备注：{memo}）' if memo else '')]
    if st in (2001, 2002, 2003):
        name = {2001: '红包', 2002: '红包', 2003: '红包封面'}[st]
        return [f'[{name}] {g("wcpayinfo/sendertitle") or des or title}'.strip()]
    if st == 8:
        return ['[动图表情]']
    if st == 24:
        return [f'[笔记] {des or title}']
    if st == 124:
        return [f'[微信礼物] {des}']
    if st == 47:
        return ['[表情]']
    label = {5: '链接', 4: '视频链接', 3: '音乐', 76: '音乐', 1: '分享文字', 92: '文章', 68: '链接',
             44: '拼团', 82: '直播商品', 93: '视频号'}.get(st, f'分享·{st}')
    if st == 1 and title:
        return [title]
    head = f'[{label}] ' + (f'[{one_line(title, 200)}]({url})' if url and title else (title or url or '(无标题)'))
    out = [head]
    if des and des != title:
        out.append(one_line(des, 300))
    app = g('appinfo/appname') or g('sourcedisplayname')
    if app:
        out[-1] += f'（来自 {app}）'
    return out


def render_sys(s):
    s = PREFIX_RE.sub('', s)
    if '<sysmsg' in s:
        r = xml_root(s)
        if r is not None:
            t = r.get('type', '')
            if t == 'revokemsg':
                return tx(r, 'revokemsg/replacemsg') or tx(r, 'revokemsg/content') or '撤回了一条消息'
            if t == 'sysmsgtemplate':
                ct = r.find('sysmsgtemplate/content_template')
                tpl = tx(ct, 'template') or tx(ct, 'plain')
                vals = {}
                for lk in (ct.findall('link_list/link') if ct is not None else []):
                    nicks = [tx(m, 'nickname') or tx(m, 'username') for m in lk.findall('memberlist/member')]
                    vals[lk.get('name')] = '、'.join(x for x in nicks if x) or tx(lk, 'title') or tx(lk, 'plain') or ''
                return re.sub(r'\$(\w+)\$', lambda m: vals.get(m.group(1), ''), tpl) or '[系统消息]'
            if t == 'delchatroommember':
                return tx(r, 'delchatroommember/plain') or tx(r, 'delchatroommember/text')
            if t == 'paymsg':
                return strip_tags(tx(r, 'content'))
            if t == 'mmchatroomtopmsg':
                nick = tx(r, 'mmchatroomtopmsg/nickname')
                op = tx(r, 'mmchatroomtopmsg/op')
                return f'{nick} {"置顶了一条消息" if op == "1" else "更新了群置顶"}'.strip()
            if t == 'pat':
                tpl = tx(r, 'pat/template')
                names = {}
                for k in ('fromusername', 'pattedusername'):
                    names[k] = tx(r, f'pat/{k}')
                return '拍一拍：' + re.sub(r'\$\{([^}]+)\}', lambda m: names.get(m.group(1), m.group(1)), tpl)
            txt = ''.join(r.itertext()).strip()
            txt = re.sub(r'\s+', ' ', txt)
            # sysmsgtemplate 里的 $占位$
            return txt[:500] or f'[系统消息 {t}]'
        return one_line(strip_tags(s), 500)
    return strip_tags(s)


def render(row, ctx):
    """返回 (kind, lines)：kind = 'msg' | 'sys'"""
    t, sub = row['type'], row['sub']
    s = row['body']
    chat = ctx['chat']
    if t == 1:
        return 'msg', s.split('\n')
    if t in (10000, 10002):
        return 'sys', [render_sys(s)]
    if t == 3:
        h = row.get('md5')
        p = ctx['media'].image(chat, h) if h else None
        tag = '[图片]'
        if p:
            tag += ' · ' + link('本地文件', p) + '（加密 .dat，需解密后才能打开）'
        return 'msg', [tag]
    if t == 43:
        r = xml_root(s)
        sec = (r.find('videomsg').get('playlength') if r is not None and r.find('videomsg') is not None else '') or ''
        tag = f'[视频{" " + sec + "秒" if sec else ""}]'
        h = row.get('md5')
        p = ctx['media'].video.get(h) if h else None
        if p:
            tag += ' · ' + link('打开视频', p)
        return 'msg', [tag]
    if t == 34:
        ms = re.search(r'voicelength\s*=\s*"(\d+)"', s)
        sec = round(int(ms.group(1)) / 1000) if ms else None
        tag = f'[语音{" " + str(sec) + "秒" if sec is not None else ""}]'
        vt = row.get('voice')
        if vt:
            tag += f' 转文字：{vt}'
        return 'msg', [tag]
    if t == 47:
        return 'msg', ['[表情]']
    if t == 42:
        r = xml_root(s)
        nick = r.get('nickname', '') if r is not None else rx(s, 'nickname')
        return 'msg', [f'[名片] {nick}'.strip()]
    if t == 48:
        r = xml_root(s)
        loc = r.find('location') if r is not None else None
        if loc is not None:
            return 'msg', [f'[位置] {loc.get("poiname", "")} · {loc.get("label", "")}'.strip(' ·')]
        return 'msg', ['[位置]']
    if t == 50:
        r = xml_root(s)
        m = tx(r, 'VoIPBubbleMsg/msg') if r is not None else ''
        return 'msg', [f'[通话] {m}'.strip()]
    if t == 49:
        return 'msg', render_app(s, sub, ctx)
    if t in (66, 67):
        r = xml_root(s)
        nick = r.get('nickname', '') if r is not None else ''
        return 'msg', [f'[企业微信名片] {nick}'.strip()]
    if t == 11000:
        return 'msg', ['[本版本不支持显示的消息]']
    if t == 37:
        return 'sys', ['好友验证消息：' + (re.search(r'content="([^"]*)"', s) or [None, ''])[1]]
    if t == 40:
        return 'msg', ['[可能认识的人]']
    if t == 52:
        return 'msg', ['[视频通话]']
    return 'msg', [f'[消息类型 {t}]']


# ---------- 主流程 ----------
def safe_name(s):
    import unicodedata
    s = ''.join(c for c in s if unicodedata.category(c)[0] != 'C' and ord(c) < 0xE0000)
    s = re.sub(r'[\\/:*?"<>|\n\r\t]', '_', s).strip(' .')
    return s[:60] or '未命名'


def category(u):
    if u.endswith('@chatroom'):
        return '群聊'
    if u.startswith('gh_'):
        return '公众号与服务号'
    if u.endswith('@openim'):
        return '企业微信'
    if u.startswith('wxid_') or '@' not in u:
        return '私聊'
    return '其他'


def load_meta():
    names, rooms = {}, {}
    usernames = set()
    with open_db('contact/contact.db') as db:
        for r in db.query('select username,nick_name,remark from contact'):
            u = r['username']
            usernames.add(u)
            names[u] = (r['remark'] or '').strip() or (r['nick_name'] or '').strip()
        for r in db.query('select username, hex(ext_buffer) b from chat_room'):
            usernames.add(r['username'])
            try:
                rooms[r['username']] = room_members(bytes.fromhex(r['b'] or ''))
            except Exception:
                rooms[r['username']] = {}
        for r in db.query('select username,nick_name from stranger'):
            if r['username'] not in names or not names[r['username']]:
                names[r['username']] = (r['nick_name'] or '').strip()
    with open_db('session/session.db') as db:
        for r in db.query('select username from SessionTable'):
            usernames.add(r['username'])
    return names, rooms, usernames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', nargs='*')
    ap.add_argument('--out', default=str(HERE / 'export'))
    args = ap.parse_args()
    out = Path(args.out)

    t0 = time.time()
    names, rooms, usernames = load_meta()
    dbs = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / 'message').glob('*message_*.db')
                 if re.fullmatch(r'(biz_)?message_\d+\.db', p.name))
    # 表 → username
    convs = defaultdict(list)   # username -> [(dbrel, table, count)]
    n2i = {}
    for rel in dbs:
        with open_db(rel) as db:
            ids = {int(r['r']): r['user_name'] for r in db.query('select rowid r,user_name from Name2Id')}
            n2i[rel] = ids
            usernames.update(ids.values())
    usernames.add('filehelper')
    lookup = {md5(u): u for u in usernames}
    for rel in dbs:
        with open_db(rel) as db:
            for r in db.query("select name from sqlite_master where type='table' and name like 'Msg_%'"):
                tb = r['name']
                u = lookup.get(tb[4:], 'unknown_' + tb[4:12])
                n = int(db.query(f'select count(*) n from "{tb}"')[0]['n'])
                if n:
                    convs[u].append((rel, tb, n))
    total = sum(n for v in convs.values() for _, _, n in v)
    print(f'会话 {len(convs)} 个，消息 {total:,} 条，准备用时 {time.time()-t0:.1f}s', flush=True)

    if args.only:
        sel = {}
        for q in args.only:
            for u in convs:
                if q == u or q == names.get(u):
                    sel[u] = convs[u]
        convs = sel
        print('只导出：', [(names.get(u) or u) for u in convs], flush=True)

    media = Media()
    me = names.get(SELF) or '我'
    index = []
    used = set()
    done_msgs = 0
    order = sorted(convs, key=lambda u: -sum(n for _, _, n in convs[u]))
    for ci, u in enumerate(order, 1):
        parts = convs[u]
        cnt = sum(n for _, _, n in parts)
        disp = names.get(u) or (u if not u.startswith('unknown_') else f'未知会话 {u[8:]}')
        cat = category(u)
        base = safe_name(disp)
        key = f'{cat}/{base}'
        if key in used:
            base = f'{base}_{md5(u)[:6]}'
        used.add(f'{cat}/{base}')
        member = rooms.get(u, {})
        ctx = {'chat': u, 'media': media}
        is_group = u.endswith('@chatroom')

        def who(sid):
            if sid == SELF:
                return f'{me}（我）'
            if not sid:
                return '未知'
            return names.get(sid) or member.get(sid) or sid
        def who_group(sid):
            if sid == SELF:
                return f'{me}（我）'
            return (names.get(sid) if sid in names and names[sid] else None) or member.get(sid) or sid or '未知'

        rows = []
        for rel, tb, _ in parts:
            ids = n2i[rel]
            with open_db(rel) as db:
                q = (f'select local_id, local_type, create_time, sort_seq, real_sender_id, hex(message_content) c, '
                     f"case when (local_type & 4294967295) in (3,43,34) then hex(packed_info_data) end p "
                     f'from "{tb}"')
                for r in db.query(q):
                    lt = int(r['local_type'])
                    t = lt & 0xFFFFFFFF
                    body = unpack(r['c'])
                    sid = ids.get(int(r['real_sender_id'] or 0))
                    m = PREFIX_RE.match(body)
                    if m and (is_group or t != 1):
                        if not sid or sid == u:
                            sid = m.group(1)
                        body = body[m.end():]
                    row = {'type': t, 'sub': lt >> 32, 'time': int(r['create_time'] or 0),
                           'seq': int(r['sort_seq'] or 0), 'sid': sid, 'body': body}
                    if r['p']:
                        pk = bytes.fromhex(r['p'])
                        if t in (3, 43):
                            mm = re.search(rb'[0-9a-f]{32}', pk)
                            row['md5'] = mm.group().decode() if mm else None
                        else:
                            row['voice'] = voice_text(pk)
                    rows.append(row)
        rows.sort(key=lambda x: (x['time'], x['seq']))

        # 分文件
        def bucket(r):
            return ''
        if cnt > SPLIT_AT:
            per_year = defaultdict(int)
            for r in rows:
                per_year[ts(r['time'], '%Y')] += 1
            def bucket(r):
                y = ts(r['time'], '%Y')
                return ts(r['time'], '%Y-%m') if per_year[y] > MONTH_SPLIT_AT else y
        groups = defaultdict(list)
        for r in rows:
            groups[bucket(r)].append(r)

        files = []
        for b in sorted(groups):
            grp = groups[b]
            if b:
                rel_path = Path(cat) / base / f'{b}.md'
            else:
                rel_path = Path(cat) / f'{base}.md'
            fp = out / rel_path
            fp.parent.mkdir(parents=True, exist_ok=True)
            with open(fp, 'w', encoding='utf-8') as f:
                f.write(f'# {disp}{" · " + b if b else ""}\n\n')
                f.write(f'- 类型：{cat}\n- 微信 ID：`{u}`\n- 本文件消息数：{len(grp):,}'
                        f'（整个会话共 {cnt:,} 条）\n- 时间范围：{ts(grp[0]["time"])} → {ts(grp[-1]["time"])}\n\n')
                day = None
                for r in grp:
                    d = ts(r['time'], '%Y-%m-%d')
                    if d != day:
                        day = d
                        f.write(f'\n## {d}\n\n')
                    try:
                        kind, lines = render(r, ctx)
                    except Exception as e:  # 单条坏数据不拖垮整体
                        kind, lines = 'msg', [f'[解析失败：{type(e).__name__}]']
                    if kind == 'sys':
                        f.write(f'*〔系统提示 {ts(r["time"], "%H:%M:%S")}〕{" ".join(x for x in lines if x)}*\n\n')
                    else:
                        name = who_group(r['sid']) if is_group else who(r['sid'])
                        f.write(f'**{name}** · {ts(r["time"])}\n')
                        f.write('\n'.join('> ' + x if x else '>' for x in lines) + '\n\n')
            files.append((b, rel_path))
        index.append({'u': u, 'name': disp, 'cat': cat, 'n': cnt,
                      'start': rows[0]['time'], 'end': rows[-1]['time'], 'files': files})
        done_msgs += cnt
        if ci % 50 == 0 or ci == len(order):
            el = time.time() - t0
            print(f'进度 {ci}/{len(order)} 会话，{done_msgs:,} 条，已用 {el/60:.1f} 分钟', flush=True)

    # README 总索引
    index.sort(key=lambda x: -x['end'])
    with open(out / 'README.md', 'w', encoding='utf-8') as f:
        f.write('# 微信聊天记录导出 · 总索引\n\n')
        f.write(f'- 导出时间：{ts(time.time())}\n- 会话数：{len(index):,}\n'
                f'- 消息总数：{sum(x["n"] for x in index):,}\n')
        if index:
            f.write(f'- 时间范围：{ts(min(x["start"] for x in index), "%Y-%m-%d")} → '
                    f'{ts(max(x["end"] for x in index), "%Y-%m-%d")}\n')
        f.write('\n说明：按最近一条消息时间倒序排列。图片目前只标注了本机加密文件位置；视频和文件可直接点开本地原件。\n\n')
        by_cat = defaultdict(int)
        for x in index:
            by_cat[x['cat']] += 1
        f.write('| 分类 | 会话数 |\n|---|---|\n' + ''.join(f'| {k} | {v} |\n' for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])))
        f.write('\n| 会话 | 分类 | 消息数 | 起 | 止 | 文件 |\n|---|---|---:|---|---|---|\n')
        for x in index:
            nm = x['name'].replace('|', '\\|')
            fl = ' '.join(f'[{b or "打开"}](<{p.as_posix()}>)' for b, p in x['files'])
            f.write(f'| {nm} | {x["cat"]} | {x["n"]:,} | {ts(x["start"], "%Y-%m-%d")} | '
                    f'{ts(x["end"], "%Y-%m-%d")} | {fl} |\n')
    print(f'完成，用时 {(time.time()-t0)/60:.1f} 分钟，输出：{out}', flush=True)


if __name__ == '__main__':
    main()
