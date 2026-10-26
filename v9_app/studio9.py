# -*- coding: utf-8 -*-
r"""Karazma Studio v9 (SOP 2.5): course download -> Stage 1 (audio, clap detection, transcription) -> [Mosi writes edit.json] -> Stage 2 (cut + render into Final).
Layout:  ROOT\start.bat   ROOT\app\ (this program, python, ffmpeg, models)   ROOT\Courses\<Course - Teacher>\<EP>\ ...   ROOT\Courses\<Course - Teacher>\Final\<EP>.mp4
"""
import os, sys, re, json, time, hashlib, shutil, subprocess, urllib.request, urllib.parse, logging, traceback, datetime, wave, zipfile

APP = os.path.dirname(os.path.abspath(__file__))                      # ROOT\app
ROOT = os.environ.get('KARAZMA_ROOT') or os.path.dirname(APP)
COURSES = os.path.join(ROOT, 'Courses')
MODELS = os.path.join(APP, 'models'); ASSETS = os.path.join(APP, 'assets')
CFG = os.path.join(APP, 'config.json'); LOG = os.path.join(APP, 'karazma-studio.log')
EXE = '.exe' if os.name == 'nt' else ''
VERSION = '9.6'
# v9.4 (Arsha 2026-10-26): cut rules - pause > PAUSE_MAX_S is cut, no kept piece shorter than MIN_KEEP_S
PAUSE_MAX_S = 1.0
MIN_KEEP_S = 1.0
PAUSE_PAD = (0.10, 0.14)   # margin kept after the last word / before the next word
RAW_EXT = ('.mp4', '.mov', '.mxf', '.mkv', '.m4v')
FINAL_DIR = 'Final'
os.makedirs(COURSES, exist_ok=True); os.makedirs(MODELS, exist_ok=True)
logging.basicConfig(filename=LOG, level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', encoding='utf-8')
try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception: pass

def say(msg, lvl='info'):
    print(msg, flush=True); getattr(logging, lvl)(msg)
def read_line(prompt):
    sys.stdout.write(prompt); sys.stdout.flush()
    line = sys.stdin.readline()
    if not line: raise EOFError('input closed')
    return line.rstrip('\r\n')
def ask(q, default=''):
    v = read_line(f'{q}' + (f' [{default}]' if default else '') + ': ').strip().strip('"'); return v or default
def yes(q, default='n'): return ask(q + ' (y/n)', default).lower().startswith('y')
def load_json(p, d=None):
    try:
        with open(p, encoding='utf-8-sig') as f: return json.load(f)
    except FileNotFoundError: return d
def save_json(p, o):
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f: json.dump(o, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''): h.update(b)
    return h.hexdigest()
def safe_name(n): return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', n).strip().rstrip('.') or '_'
def fmt(t):
    t = max(0.0, float(t)); m = int(t // 60); return f'{m:02d}:{t - 60 * m:04.1f}'
_last = [None]
def bar(done, total, label):
    if not total: return
    pc = int(100 * done // total)
    if _last[0] == (label, pc): return
    _last[0] = (label, pc); n = int(30 * done / total); print(f'\r  {label} [{"#" * n}{"." * (30 - n)}] {pc:3d}%', end='', flush=True)

# ---------- ffmpeg ----------
V9_DRIVE_ROOT = '1fvAAdI0kUTRnISd5WLC5h2MNnLvgB4Eb'   # karazma-studio-full-operator (anyone with the link): v9's own update / asset / model source
GH_OWNER = 'Vaguepure'
GH_REPO = 'karazma-studio-tuts'
GH_BRANCH = 'main'
GH_APP_DIR = 'v9_app'   # path inside the repo for operator app files
# v9.6+: app code updates from public GitHub raw URLs (no token). Drive kept for .assets / ffmpeg / models.
def find_ff(name):
    p = os.path.join(APP, 'ffmpeg', name + EXE)
    return p if os.path.isfile(p) else (shutil.which(name) if os.name != 'nt' else None)
FFMPEG = find_ff('ffmpeg'); FFPROBE = find_ff('ffprobe')
def need_ff():
    global FFMPEG, FFPROBE
    if not FFMPEG or not FFPROBE:
        say('ffmpeg/ffprobe not found in app\\ffmpeg - downloading them from the Karazma Studio v9 Drive folder...')
        try: fetch_ffmpeg()
        except Exception as e: logging.error(traceback.format_exc()); say(f'   could not download ffmpeg: {e}', 'warning')
        FFMPEG = find_ff('ffmpeg'); FFPROBE = find_ff('ffprobe')
    if not FFMPEG or not FFPROBE:
        raise RuntimeError('ffmpeg.exe / ffprobe.exe not found. Extract karazma-studio-v9_4_ffmpeg.zip and karazma-studio-v9_5_ffprobe.zip (from the v9 Drive folder) into the same folder as the other v9 zips.')
NOWIN = 0x08000000 if os.name == 'nt' else 0   # CREATE_NO_WINDOW
BELOW_NORMAL = 0x00004000 if os.name == 'nt' else 0  # BELOW_NORMAL_PRIORITY_CLASS — leave UI responsive

# ---------- resource throttling (v9.1: avoid pegging every core / BSOD on mid-range PCs) ----------
def _cpu_n():
    return max(1, os.cpu_count() or 4)
def default_thread_cap():
    """Safe default: leave headroom for Windows (never use all cores).
    Mid-range (4–8 cores): about half the cores, leave at least 2 free when possible."""
    n = _cpu_n()
    if n <= 2: return 1
    if n <= 4: return max(1, n // 2)         # 4-core → 2 (leave 2 for Windows/UI)
    return max(1, min(n // 2, n - 2))        # ~half, leave ≥2
def cfg_int(key, default=None):
    c = load_json(CFG, {}) or {}
    v = os.environ.get('KARAZMA_' + key.upper()) or c.get(key)
    if v is None or v == '': return default
    try: return int(v)
    except (TypeError, ValueError): return default
def whisper_cpu_threads():
    """CTranslate2 / faster-whisper worker threads. config: whisper_cpu_threads | threads | max_cpu_threads"""
    c = load_json(CFG, {}) or {}
    want = os.environ.get('KARAZMA_THREADS') or c.get('whisper_cpu_threads') or c.get('threads') or c.get('max_cpu_threads')
    cap = _cpu_n() if c.get('allow_all_cores') else default_thread_cap()
    if want is None or want == '': return max(1, cap)
    return max(1, min(int(want), _cpu_n() if c.get('allow_all_cores') else cap))
def ffmpeg_threads():
    """ffmpeg -threads. config: ffmpeg_threads (default min(4, whisper cap))"""
    c = load_json(CFG, {}) or {}
    want = c.get('ffmpeg_threads')
    cap = min(4, default_thread_cap())
    if want is None or want == '': return max(1, cap)
    return max(1, min(int(want), _cpu_n()))
def lower_priority_enabled():
    c = load_json(CFG, {}) or {}
    if 'lower_priority' in c: return bool(c['lower_priority'])
    return True
def apply_resource_env(threads=None):
    """Cap OpenMP / BLAS / etc. so they cannot multiply past our Whisper thread budget."""
    t = str(threads if threads is not None else whisper_cpu_threads())
    for k in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS',
              'VECLIB_MAXIMUM_THREADS', 'OPENMP_NUM_THREADS'):
        os.environ[k] = t
    os.environ.setdefault('OMP_WAIT_POLICY', 'PASSIVE')   # less busy-wait spinning
    os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
    os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS_WARNING', '1')
    return int(t)
def creation_flags():
    f = NOWIN
    if lower_priority_enabled(): f |= BELOW_NORMAL
    return f
def lower_own_priority():
    """Drop THIS python process below normal on Windows so Explorer/UI stay alive during Stage 1/2."""
    if os.name != 'nt' or not lower_priority_enabled(): return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        # BELOW_NORMAL_PRIORITY_CLASS
        k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000)
    except Exception as e:
        logging.info('could not lower own priority: %s', e)
def free_ram_gb():
    """Best-effort free RAM in GiB (Windows via GlobalMemoryStatusEx; else None)."""
    try:
        if os.name == 'nt':
            import ctypes
            class M(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                            ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                            ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                            ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                            ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
            m = M(); m.dwLength = ctypes.sizeof(M)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return m.ullAvailPhys / (1024 ** 3)
        else:
            with open('/proc/meminfo') as f:
                for line in f:
                    if line.startswith('MemAvailable:'):
                        return int(line.split()[1]) / (1024 ** 2)
    except Exception:
        return None
    return None
def warn_model_ram(name):
    """large-v3 + int8/int16 needs several GB free; warn (do not block) if low."""
    need = {'large-v3': 6.0, 'medium': 3.0, 'small': 1.5}.get(name, 3.0)
    free = free_ram_gb()
    if free is None: return
    if free < need:
        say(f'   WARNING: only ~{free:.1f} GB free RAM; {name} wants about {need:.0f} GB free. '
            f'Prefer model medium (menu [1]) on this PC, close other apps, or the OS may restart.', 'warning')
    elif name == 'large-v3':
        say(f'   note: large-v3 is heavy (~{need:.0f} GB free recommended; you have ~{free:.1f} GB). medium is safer on mid-range PCs.')
def say_resource_plan(model_name=None):
    wt = whisper_cpu_threads(); ft = ffmpeg_threads()
    say(f'   resources: CPU={_cpu_n()} cores → whisper_threads={wt}, ffmpeg_threads={ft}, '
        f'priority={"below-normal" if lower_priority_enabled() else "normal"}'
        + (f', model={model_name}' if model_name else ''))
    say('   (override in app\\config.json: whisper_cpu_threads, ffmpeg_threads, max_cpu_threads, lower_priority, allow_all_cores)')
def inject_ff_threads(cmd):
    """Insert -threads N after the ffmpeg executable if missing (keeps encode from eating every core)."""
    if not cmd: return list(cmd)
    cmd = list(cmd)
    exe = os.path.basename(cmd[0]).lower()
    if exe not in ('ffmpeg', 'ffmpeg.exe') or '-threads' in cmd: return cmd
    return [cmd[0], '-threads', str(ffmpeg_threads())] + cmd[1:]
def run(cmd, what):
    cmd = inject_ff_threads(cmd)
    logging.info('run %s', cmd)
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=creation_flags())
    try: out, err = p.communicate()
    except BaseException: p.kill(); p.wait(); raise
    if p.returncode != 0: raise RuntimeError(f'{what} failed (ffmpeg code {p.returncode}):\n' + err.decode('utf-8', 'replace')[-1500:])
    return out, err.decode('utf-8', 'replace')
def probe(path):
    out, _ = run([FFPROBE, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', path], 'ffprobe')
    j = json.loads(out); v = next((s for s in j['streams'] if s['codec_type'] == 'video'), None)
    fps = 0.0
    if v:
        n, d = (v.get('avg_frame_rate') or '0/1').split('/'); fps = float(n) / float(d or 1)
    return dict(duration=float(j['format']['duration']), width=v['width'] if v else 0, height=v['height'] if v else 0, fps=fps,
                audio=any(s['codec_type'] == 'audio' for s in j['streams']))

# ---------- course folders ----------
def courses():
    return sorted(d for d in os.listdir(COURSES) if os.path.isdir(os.path.join(COURSES, d)) and not d.startswith('.'))
def episodes(course):
    cdir = os.path.join(COURSES, course)
    return sorted(d for d in os.listdir(cdir) if os.path.isdir(os.path.join(cdir, d)) and d != FINAL_DIR and not d.startswith(('.', '_')))
def ep_dir(course, ep): return os.path.join(COURSES, course, ep)
def raw_file(course, ep):
    d = ep_dir(course, ep)
    fs = [f for f in os.listdir(d) if f.lower().endswith(RAW_EXT) and os.path.isfile(os.path.join(d, f)) and not f.lower().startswith(ep.lower() + '_cut') and not re.match(r'^\d+_', f)]
    fs.sort(key=lambda f: (os.path.splitext(f)[0].lower() != ep.lower(), f))
    return os.path.join(d, fs[0]) if fs else None
def st_path(course, ep): return os.path.join(ep_dir(course, ep), 'work', 'status.json')
def english_only(t, default='previously edited'):
    """v9.3: console shows English only; non-ASCII (e.g. Persian) reasons become a default English text."""
    t = str(t or '').strip()
    return t if t and all(ord(ch) < 128 for ch in t) else default
def skip_info(edit):
    """v9.2: Mosi marks an episode SKIP with an edit.json like {"episode":"C5030","skip":true,"reason":"..."} -> no cut/render."""
    try: E = json.loads(open(edit, 'rb').read().decode('utf-8-sig'))
    except Exception: return None
    if isinstance(E, dict) and (E.get('skip') is True or str(E.get('status', '')).lower() == 'skip'): return english_only(E.get('reason'))
    return None
def ep_status(course, ep):
    d = ep_dir(course, ep); st = load_json(st_path(course, ep), {}) or {}
    final = os.path.join(COURSES, course, FINAL_DIR, ep + '.mp4'); edit = os.path.join(d, ep + '.edit.json')
    if os.path.exists(edit):
        why = skip_info(edit)
        if why is not None: return 'skip', f'SKIP (Mosi): {why}'
    if os.path.exists(final) and os.path.exists(edit) and st.get('final_edit_sha') == sha256(edit): return 'done', 'DONE - final video in Final folder'
    if os.path.exists(edit): return 'log', 'LOG READY - run Stage 2'
    if st.get('stage1') == 'done' and os.path.exists(os.path.join(d, ep + '-transcribe.txt')): return 'sent', 'transcribed - waiting for Mosi\'s edit log'
    if os.path.exists(os.path.join(d, ep + '.download.json')) and not raw_file(course, ep): return 'dl', 'waiting for download (menu 1)'
    if not raw_file(course, ep): return 'noraw', 'no raw video in the folder'
    tr = st.get('tr_done_chunks'); n = st.get('tr_chunks')
    if tr and n: return 'raw', f'Stage 1 paused ({len(tr)}/{n} parts transcribed) - run Stage 1 to continue'
    return 'raw', 'raw video ready - run Stage 1'

# ---------- 1) download a course (host message / Google Drive folder) + parts ----------
QUOTES = '"\u201c\u201d\u00ab\u00bb\'\u2018\u2019'
END_WORDS = ('\u067e\u0627\u06cc\u0627\u0646 \u062f\u0648\u0631\u0647', 'END')
def convert_link(u):
    u = u.strip().strip('<>()[]' + QUOTES).rstrip('.,;\u060c')
    m = re.match(r'^ftpe?s?://([^@/]+)@([^/]+)(/.*)$', u, re.I)
    if m:
        user = urllib.parse.unquote(m.group(1)).split('@')[0]; host = m.group(2).split(':')[0]
        return f'https://{host}/{user}' + urllib.parse.quote(urllib.parse.unquote(m.group(3)))
    return u
def parse_message(text):
    """first non-empty line = "<Course> - <Teacher>" (quotes optional, optional '\u0634\u0631\u0648\u0639 \u062f\u0648\u0631\u0647'); then links until '\u067e\u0627\u06cc\u0627\u0646 \u062f\u0648\u0631\u0647'"""
    lines = [l.strip() for l in text.replace('\r', '').split('\n')]
    name = None; links = []
    for l in lines:
        if not l: continue
        if any(l.startswith(w) or l == w for w in END_WORDS): break
        found = re.findall(r'(?:ftpe?s?|https?)://[^\s<>"\u201c\u201d\u00ab\u00bb]+', l, re.I)
        if found:
            for u in found:
                c = convert_link(u)
                if c.lower().endswith(RAW_EXT) and c not in links: links.append(c)
        elif name is None:
            n = re.sub(r'^\s*\u0634\u0631\u0648\u0639\s*\u062f\u0648\u0631\u0647\s*[:\-]?\s*', '', l).strip()
            m = re.search('[' + QUOTES + r']\s*(.+?)\s*[' + QUOTES + ']', n)
            name = (m.group(1) if m else n.strip(QUOTES)).strip()
    return (safe_name(name) if name else None), links
def ep_code(fn):
    b = os.path.splitext(os.path.basename(fn))[0]; m = re.match(r'^(C\d{4})', b, re.I)
    return safe_name(m.group(1).upper() if m else b)
def ep_from_link(u):
    fn = safe_name(urllib.parse.unquote(u.rsplit('/', 1)[-1].split('?')[0])); return ep_code(fn), fn
UA = {'User-Agent': 'Mozilla/5.0 KarazmaStudio/9.6'}
def download(url, dest, label, size=None, sha=None):
    if os.path.exists(dest) and (size is None or os.path.getsize(dest) == size) and (sha is None or sha256(dest) == sha): return
    tmp = dest + '.part'
    for attempt in range(1, 11):
        try:
            have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            if size is not None and have > size: os.remove(tmp); have = 0
            if not (size is not None and have == size):
                hdr = dict(UA)
                if have: hdr['Range'] = f'bytes={have}-'
                with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=120) as r:
                    code = getattr(r, 'status', 200); cl = int(r.headers.get('Content-Length') or 0)
                    first = r.read(1 << 16)
                    if first.lstrip()[:15].lower().startswith((b'<!doctype', b'<html')):
                        page = (first + r.read()).decode('utf-8', 'replace'); m = re.search(r'name="uuid" value="([^"]+)"', page)
                        if 'drive.usercontent.google.com' in url and m:
                            url = re.sub(r'&uuid=[^&]*', '', url) + '&uuid=' + m.group(1); raise IOError('Drive asked for confirmation, retrying')
                        raise IOError('server returned a web page instead of the file (wrong link, or the Drive folder is not shared "Anyone with the link")')
                    if have and code == 206:
                        m = re.search(r'/(\d+)$', r.headers.get('Content-Range', '')); total = int(m.group(1)) if m else have + cl; mode = 'ab'
                        say(f'  resuming {label} from {have >> 20} MB')
                    else: total = size or cl; have = 0; mode = 'wb'
                    got = have
                    with open(tmp, mode) as f:
                        f.write(first); got += len(first); bar(got, total, label)
                        while True:
                            b = r.read(1 << 20)
                            if not b: break
                            f.write(b); got += len(b); bar(got, total, label)
                    print()
                    if total and os.path.getsize(tmp) != total: raise IOError(f'incomplete: {os.path.getsize(tmp)} of {total} bytes')
            if size is not None and os.path.getsize(tmp) != size: raise IOError(f'incomplete: {os.path.getsize(tmp)} of {size} bytes')
            if sha and sha256(tmp) != sha: os.remove(tmp); raise IOError('SHA256 mismatch, downloading again')
            os.replace(tmp, dest); return
        except urllib.error.HTTPError as e:
            if e.code == 416 and os.path.exists(tmp) and size is None: os.replace(tmp, dest); return
            if e.code in (401, 403, 404): raise IOError(f'server said {e.code} {e.reason} for {label}')
            print(); say(f'  download problem, retry {attempt}/10: {e}', 'warning'); time.sleep(min(60, 5 * attempt))
        except Exception as e:
            print(); say(f'  download problem, retry {attempt}/10: {e}', 'warning'); time.sleep(min(60, 5 * attempt))
    raise IOError(f'download failed after 10 tries: {label} (choose the same option again later; it continues where it stopped)')
def read_message():
    print('\nPaste the message exactly as you got it:\n   "<Course Finglish> - <Teacher Finglish>"\n   link 1\n   link 2\n   ...\n   (end-of-course line)')
    print('(If the message has no end-of-course line, type END on a new line and press Enter.)')
    lines = []
    while True:
        l = read_line('')
        lines.append(l)
        if any(l.strip().startswith(w) for w in END_WORDS): break
    return '\n'.join(lines)
def host_course(text=None):
    name, links = parse_message(text if text is not None else read_message())
    if not name: name = safe_name(ask('Course folder name not found. Type it as  Course Name - Teacher Name'))
    if not links: say('No video links found in the message. Nothing to download.'); return None
    cdir = os.path.join(COURSES, name); os.makedirs(os.path.join(cdir, FINAL_DIR), exist_ok=True)
    for u in links:
        ep, fn = ep_from_link(u); d = os.path.join(cdir, ep); os.makedirs(d, exist_ok=True)
        save_json(os.path.join(d, ep + '.download.json'), {'url': u, 'file': fn, 'source': 'host', 'added': datetime.datetime.now().isoformat(timespec='seconds')})
    say(f'Course folder: {cdir}\n{len(links)} episode folder(s): ' + ', '.join(ep_from_link(u)[0] for u in links))
    download_pending(name); return name
def download_pending(only=None):
    todo = []
    for c in courses():
        if only and c != only: continue
        for ep in episodes(c):
            j = load_json(os.path.join(ep_dir(c, ep), ep + '.download.json'))
            if j and j.get('url') and not os.path.exists(os.path.join(ep_dir(c, ep), j['file'])): todo.append((c, ep, j))
    if not todo: say('All raw videos of this course are already downloaded.'); return
    ok = 0
    for i, (c, ep, j) in enumerate(todo, 1):
        say(f'\n[{i}/{len(todo)}] downloading {c} | {j["file"]}')
        try: download(j['url'], os.path.join(ep_dir(c, ep), j['file']), j['file']); ok += 1
        except Exception as e: say(f'  ERROR: {e}', 'error')
    say(f'\nDownload finished: {ok} of {len(todo)} OK.' + ('' if ok == len(todo) else ' Choose 1 again later to continue the rest.'))
# --- Google Drive (public folder, "Anyone with the link") ---
ENTRY = re.compile(r'href="https://drive\.google\.com/(file/d|drive/(?:u/\d+/)?folders)/([\w-]+)[^"]*"[\s\S]*?class="flip-entry-title">([^<]+)<')
def folder_id(s):
    m = re.search(r'folders/([\w-]+)', s) or re.search(r'[?&]id=([\w-]+)', s); return m.group(1) if m else s.strip()
def list_folder(fid):
    import html as H
    with urllib.request.urlopen(urllib.request.Request(f'https://drive.google.com/embeddedfolderview?id={fid}', headers=UA), timeout=60) as r: t = r.read().decode('utf-8', 'replace')
    m = re.search(r'<title>([^<]*)</title>', t); title = H.unescape(m.group(1)).strip() if m else ''
    return title, [{'id': x.group(2), 'name': H.unescape(x.group(3)).strip(), 'folder': x.group(1) != 'file/d'} for x in ENTRY.finditer(t)]
def gdl(fid): return f'https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t'
PART_RX = re.compile(r'^(\d+)_(.+)$')
def drive_course(link=None):
    link = link or ask('Paste the Google Drive FOLDER link of the course')
    fid = folder_id(link)
    try: title, items = list_folder(fid)
    except Exception as e: say(f'Could not open the Drive folder ({e}). Is it shared "Anyone with the link"?', 'error'); return None
    name = safe_name(re.sub(r'\s*-\s*Google Drive\s*$', '', title)) if title else ''
    if not name or name.lower() in ('google drive', 'meet google drive'): name = safe_name(ask('Folder name not readable. Type the course name (Course Name - Teacher Name)'))
    say(f'Course: {name}'); cdir = os.path.join(COURSES, name); os.makedirs(os.path.join(cdir, FINAL_DIR), exist_ok=True)
    files = [i for i in items if not i['folder']]
    for sub in [i for i in items if i['folder']]:      # one subfolder per episode is also fine
        try: files += [dict(f, sub=sub['name']) for f in list_folder(sub['id'])[1] if not f['folder']]
        except Exception as e: say(f'  could not list subfolder {sub["name"]}: {e}', 'warning')
    only = [x for x in os.environ.get('KARAZMA_ONLY_EPS', '').split(',') if x]   # test hook
    if only: files = [f for f in files if ep_code(f['name'][3:] if PART_RX.match(f['name']) else f['name']) in only]
    names = {f['name']: f for f in files}; jobs = []; used = set()
    for mn in [n for n in names if n.endswith('.manifest.json')]:      # episodes sent in PARTS (manifest + NN_<file>)
        epf = mn[:-len('.manifest.json')]; ep = ep_code(epf); d = os.path.join(cdir, ep); os.makedirs(d, exist_ok=True)
        download(gdl(names[mn]['id']), os.path.join(d, mn), mn); used.add(mn)
        man = load_json(os.path.join(d, mn)); missing = [p['name'] for p in man['parts'] if p['name'] not in names]
        if missing: say(f'  {ep}: {len(missing)} part(s) missing on Drive ({", ".join(missing[:3])}...) - skipped for now', 'warning'); continue
        jobs.append(('parts', ep, d, man)); used |= {p['name'] for p in man['parts']}
    for n, f in names.items():
        if n in used: continue
        if n.lower().endswith(RAW_EXT) and not PART_RX.match(n): ep = ep_code(n); d = os.path.join(cdir, ep); os.makedirs(d, exist_ok=True); jobs.append(('whole', ep, d, f))
        elif n.endswith('.edit.json') or n.endswith('_logs.zip'): jobs.append(('log', ep_code(n), cdir, f))
    if not jobs: say('No videos found in that Drive folder.'); return name
    ok = 0
    for i, (kind, ep, d, x) in enumerate(jobs, 1):
        try:
            if kind == 'whole':
                say(f'\n[{i}/{len(jobs)}] {ep}: {x["name"]} (whole file)'); download(gdl(x['id']), os.path.join(d, x['name']), x['name'])
            elif kind == 'parts':
                say(f'\n[{i}/{len(jobs)}] {ep}: {len(x["parts"])} parts'); pdir = os.path.join(d, 'parts'); os.makedirs(pdir, exist_ok=True)
                if not merged_ok(d, x):
                    for p in x['parts']: download(gdl(names[p['name']]['id']), os.path.join(pdir, p['name']), p['name'], p['size'], p.get('sha256'))
                    merge_parts(d, x)
            else:
                tmp = os.path.join(cdir, '_incoming'); os.makedirs(tmp, exist_ok=True); download(gdl(x['id']), os.path.join(tmp, x['name']), x['name']); import_logs(name)
            ok += 1
        except Exception as e: logging.error(traceback.format_exc()); say(f'  ERROR: {e}', 'error')
    say(f'\nDrive download finished: {ok} of {len(jobs)} OK.' + ('' if ok == len(jobs) else ' Choose 1 again later; finished files are not downloaded again.'))
    return name
# --- parts (v8 style: NN_<file> + <file>.manifest.json with size + SHA256) ---
def merged_ok(d, man):
    f = os.path.join(d, os.path.basename(man['episode']))
    return os.path.exists(f) and os.path.getsize(f) == man['total_size'] and load_json(os.path.join(d, 'work', 'merged.json'), {}).get(man['sha256']) == os.path.getsize(f)
def merge_parts(d, man):
    """merge <d>\\parts\\NN_<file> (or parts in <d> itself) -> <d>\\<file>; every part and the result checked (size + SHA256); parts deleted after a good merge"""
    out = os.path.join(d, os.path.basename(man['episode']))
    if merged_ok(d, man): return out
    parts = sorted(man['parts'], key=lambda p: p.get('index', int(p['name'].split('_', 1)[0]))); bad = []; paths = []
    for p in parts:
        f = next((x for x in (os.path.join(d, 'parts', p['name']), os.path.join(d, p['name'])) if os.path.exists(x)), None)
        if not f: bad.append(p['name'] + ': MISSING'); continue
        if os.path.getsize(f) != p['size'] or (p.get('sha256') and sha256(f) != p['sha256']): bad.append(p['name'] + ': damaged (size/SHA256)'); continue
        paths.append(f)
    if bad: raise IOError('parts problem:\n   ' + '\n   '.join(bad))
    tmp = out + '.tmp'; done = 0
    with open(tmp, 'wb') as o:
        for f in paths:
            with open(f, 'rb') as i:
                for b in iter(lambda: i.read(8 << 20), b''): o.write(b); done += len(b); bar(done, man['total_size'], 'merge')
    print()
    if os.path.getsize(tmp) != man['total_size'] or sha256(tmp) != man['sha256']: os.remove(tmp); raise IOError('merged file does not match the manifest (SHA256)')
    os.replace(tmp, out); os.makedirs(os.path.join(d, 'work'), exist_ok=True)
    mj = os.path.join(d, 'work', 'merged.json'); m = load_json(mj, {}) or {}; m[man['sha256']] = man['total_size']; save_json(mj, m)
    for f in paths:
        try: os.remove(f)
        except OSError: pass
    say(f'   merged {len(paths)} parts -> {os.path.basename(out)} (size + SHA256 OK)'); return out
def merge_local_parts(course):
    """parts the operator copied by hand into an episode folder are merged automatically"""
    for ep in episodes(course):
        d = ep_dir(course, ep)
        for mn in [f for f in os.listdir(d) if f.endswith('.manifest.json')]:
            man = load_json(os.path.join(d, mn))
            if man and man.get('parts') and not merged_ok(d, man):
                try: merge_parts(d, man)
                except Exception as e: say(f'   {ep}: {e}', 'warning')
def split_parts(path, size=95 << 20):
    """split a big file into NN_<name> + <name>.manifest.json next to it (for transfers with a size cap)"""
    name = os.path.basename(path); d = os.path.dirname(path); total = os.path.getsize(path); parts = []
    with open(path, 'rb') as f:
        k = 1
        while True:
            b = f.read(size)
            if not b: break
            pn = f'{k:02d}_{name}'
            with open(os.path.join(d, pn), 'wb') as o: o.write(b)
            parts.append(dict(index=k, name=pn, size=len(b), sha256=hashlib.sha256(b).hexdigest())); k += 1
    save_json(os.path.join(d, name + '.manifest.json'), dict(episode=name, total_size=total, sha256=sha256(path), parts=parts))
    return [os.path.join(d, p['name']) for p in parts]
def import_logs(course):
    """Mosi's logs: <EP>.edit.json files or a <Course>_logs.zip dropped in the course folder (or _incoming) -> each episode folder"""
    cdir = os.path.join(COURSES, course); n = 0
    srcs = [cdir, os.path.join(cdir, '_incoming')]
    for sd in srcs:
        if not os.path.isdir(sd): continue
        for f in sorted(os.listdir(sd)):
            p = os.path.join(sd, f)
            if f.lower().endswith('.zip') and 'logs' in f.lower():
                with zipfile.ZipFile(p) as z:
                    for zi in z.infolist():
                        bn = os.path.basename(zi.filename)
                        if bn.lower() == 'skip_episodes.json':   # v9.2: {"skip":[{"episode":"C5030","reason":"..."}, "C5033", ...]}
                            L = json.loads(z.read(zi).decode('utf-8-sig')); L = L.get('skip', []) if isinstance(L, dict) else L
                            for it in L:
                                ep = ep_code(str(it.get('episode') if isinstance(it, dict) else it)); d = ep_dir(course, ep)
                                if not os.path.isdir(d):   # accept "C5030" for folder "02 - C5030"
                                    m = [x for x in episodes(course) if re.search(r'(^|\W)' + re.escape(ep) + r'$', x, re.I)]
                                    if len(m) == 1: ep = m[0]; d = ep_dir(course, ep)
                                if not os.path.isdir(d): say(f'   skip {ep}: no episode folder in this course - ignored', 'warning'); continue
                                why = english_only(it.get('reason') if isinstance(it, dict) else None)
                                save_json(os.path.join(d, ep + '.edit.json'), dict(episode=ep, skip=True, reason=why)); n += 1
                            continue
                        if bn.endswith('.edit.json'):
                            ep = ep_code(bn[:-len('.edit.json')]); d = ep_dir(course, ep)
                            if not os.path.isdir(d): say(f'   log {bn}: no episode folder {ep} in this course - skipped', 'warning'); continue
                            data = z.read(zi)
                            json.loads(data.decode('utf-8-sig'))  # must be valid JSON
                            dst = os.path.join(d, ep + '.edit.json')
                            if not (os.path.exists(dst) and open(dst, 'rb').read() == data):
                                with open(dst + '.tmp', 'wb') as o: o.write(data)
                                os.replace(dst + '.tmp', dst); n += 1
                os.replace(p, p + '.imported')
            elif f.endswith('.edit.json'):
                ep = ep_code(f[:-len('.edit.json')]); d = ep_dir(course, ep)
                if os.path.isdir(d): shutil.move(p, os.path.join(d, ep + '.edit.json')); n += 1
    if n: say(f'   {n} edit log(s) placed in the episode folders.')
    return n

# ---------- v9 Drive folder: updates, assets, ffmpeg, model fallback ----------
def drive_tree(fid, prefix=''):
    out = {}
    for i in list_folder(fid)[1]:
        if i['folder']: out.update(drive_tree(i['id'], prefix + i['name'] + '/'))
        else: out[prefix + i['name']] = i['id']
    return out
_ROOT_CACHE = {}
def v9_root():
    if 'items' not in _ROOT_CACHE: _ROOT_CACHE['items'] = {i['name']: i for i in list_folder(V9_DRIVE_ROOT)[1]}
    return _ROOT_CACHE['items']
def fetch_ffmpeg():
    items = v9_root()
    for name, zn in (('ffmpeg', 'karazma-studio-v9_4_ffmpeg.zip'), ('ffprobe', 'karazma-studio-v9_5_ffprobe.zip')):
        dst = os.path.join(APP, 'ffmpeg', name + EXE)
        if os.path.isfile(dst): continue
        if zn not in items: raise IOError(zn + ' is not in the v9 Drive folder')
        os.makedirs(os.path.dirname(dst), exist_ok=True); zp = os.path.join(APP, 'ffmpeg', zn)
        download(gdl(items[zn]['id']), zp, zn)
        with zipfile.ZipFile(zp) as z:
            m = next(n for n in z.namelist() if n.lower().endswith('/' + name + EXE) or n.lower() == name + EXE)
            with z.open(m) as i, open(dst + '.tmp', 'wb') as o: shutil.copyfileobj(i, o, 8 << 20)
        os.replace(dst + '.tmp', dst); os.remove(zp); say(f'   {name}{EXE} ready')
def github_raw_url(rel):
    """Public raw URL for a file under v9_app/ on the GitHub repo. Repo is public -> no token needed."""
    rel = rel.replace('\\', '/').lstrip('/')
    return f'https://raw.githubusercontent.com/{GH_OWNER}/{GH_REPO}/{GH_BRANCH}/{GH_APP_DIR}/{rel}'
def sync_app_from_github():
    """v9.6: update operator app files from GitHub (app_manifest.json + listed files). Returns True if code changed."""
    code_changed = False
    try:
        mp = os.path.join(APP, 'app_manifest.json.new')
        download(github_raw_url('app_manifest.json'), mp, 'app_manifest.json')
        man = load_json(mp, {}) or {}; files = man.get('files') or {}
        if not files: raise IOError('app_manifest.json has no files list')
        changed = []
        for rel, f in files.items():
            dest = os.path.join(APP, *rel.split('/')); os.makedirs(os.path.dirname(dest) or APP, exist_ok=True)
            if os.path.exists(dest) and os.path.getsize(dest) == f.get('bytes') and sha256(dest) == f.get('sha256'): continue
            download(github_raw_url(rel), dest + '.new', rel, f.get('bytes'), f.get('sha256')); changed.append((dest + '.new', dest))
        for a, b in changed: os.replace(a, b)
        os.replace(mp, os.path.join(APP, 'app_manifest.json'))
        if changed:
            say(f'Updated {len(changed)} file(s) from GitHub ({GH_OWNER}/{GH_REPO}@{GH_BRANCH}/{GH_APP_DIR}, version {man.get("version", "?")}).')
            code_changed = True
    except Exception as e:
        logging.error(traceback.format_exc()); say(f'(could not update app from GitHub: {e}; using the files already on this PC)', 'warning')
    return code_changed
def sync_assets_from_drive():
    """Base renderer assets still live on the v9 Drive folder (.assets). Never fatal."""
    try: items = v9_root()
    except Exception as e: say(f'(Drive not reachable for assets: {e}; using the assets already on this PC)', 'warning'); return
    folder, local, mname = '.assets', ASSETS, 'assets_manifest.json'
    try:
        if folder not in items: return
        tree = drive_tree(items[folder]['id'])
        if mname not in tree: return
        mp = os.path.join(local, mname + '.new'); download(gdl(tree[mname]), mp, mname)
        man = load_json(mp, {}) or {}; files = man.get('files') or {v['path']: v for v in man.get('assets', {}).values()}; changed = []
        for rel, f in files.items():
            dest = os.path.join(local, *rel.split('/')); os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.exists(dest) and os.path.getsize(dest) == f.get('bytes') and sha256(dest) == f.get('sha256'): continue
            if rel not in tree: raise IOError(f'{rel} missing in Drive {folder}')
            download(gdl(tree[rel]), dest + '.new', rel, f.get('bytes'), f.get('sha256')); changed.append((dest + '.new', dest))
        for a, b in changed: os.replace(a, b)
        os.replace(mp, os.path.join(local, mname))
        if changed: say(f'Updated {len(changed)} asset file(s) from Drive (version {man.get("version", "?")}).')
    except Exception as e:
        logging.error(traceback.format_exc()); say(f'(could not update assets from Drive: {e}; using the assets already on this PC)', 'warning')
def sync_from_drive():
    """v9.6: app code from GitHub; .assets still from Drive. Returns True when program code changed (for auto-restart)."""
    code_changed = sync_app_from_github()
    sync_assets_from_drive()
    return code_changed
def restart_program(args=None):
    """v9.5: relaunch the same python/exe with the same arguments in the same console window, then exit with its code.
    KARAZMA_RESTARTED=1 stops a restart loop (the new process will not auto-restart a second time)."""
    if os.environ.get('KARAZMA_RESTARTED') == '1':
        say('New code downloaded, but this run was already restarted once; please close and reopen the program.', 'warning'); return
    args = list(sys.argv[1:] if args is None else args)
    cmd = [sys.executable] + ([] if getattr(sys, 'frozen', False) else [os.path.abspath(sys.argv[0])]) + args
    say('Update installed. Restarting Karazma Studio automatically ...')
    logging.info('restart: %s', cmd)
    env = dict(os.environ, KARAZMA_RESTARTED='1')
    try:
        for h in logging.getLogger().handlers: h.flush()
        rc = subprocess.call(cmd, env=env)
    except Exception as e:
        logging.error(traceback.format_exc()); say(f'Automatic restart failed ({e}); please close and reopen the program.', 'warning'); return
    sys.exit(rc)
# model files: known-good SHA256 of model.bin (= Hugging Face LFS etag), checked whatever the source
MODEL_FILES = {
    'small': dict(sha='3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671', size=483546902, files=['config.json', 'tokenizer.json', 'vocabulary.txt']),
    'medium': dict(sha='9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae', size=1527906378, files=['config.json', 'tokenizer.json', 'vocabulary.txt']),
    'large-v3': dict(sha='69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1', size=3087284237, files=['config.json', 'preprocessor_config.json', 'tokenizer.json', 'vocabulary.json'])}
MIRRORS = [('Hugging Face', 'https://huggingface.co/Systran/faster-whisper-{m}/resolve/main/{f}'),
           ('hf-mirror.com', 'https://hf-mirror.com/Systran/faster-whisper-{m}/resolve/main/{f}'),
           ('ModelScope', 'https://www.modelscope.cn/models/Systran/faster-whisper-{m}/resolve/master/{f}')]
def fetch_model(name):
    info = MODEL_FILES[name]; dst = os.path.join(MODELS, 'faster-whisper-' + name); os.makedirs(dst, exist_ok=True)
    mb = os.path.join(dst, 'model.bin')
    def complete(): return os.path.exists(mb) and os.path.getsize(mb) == info['size'] and all(os.path.exists(os.path.join(dst, f)) for f in info['files'])
    c = load_json(CFG, {}) or {}; order = list(MIRRORS)
    if c.get('model_mirror'): order.insert(0, ('custom', c['model_mirror']))
    for label, pat in order:
        if complete(): break
        say(f'   trying {label} ...')
        try:
            for f in info['files']: download(pat.format(m=name, f=f), os.path.join(dst, f), f'{name}/{f}')
            if not (os.path.exists(mb) and sha256(mb) == info['sha']):
                if os.path.exists(mb): os.remove(mb)
                download(pat.format(m=name, f='model.bin'), mb, f'{name}/model.bin', info['size'], info['sha'])
        except Exception as e: say(f'   {label} did not work: {str(e)[:160]}', 'warning')
    if not complete():
        say('   trying the Karazma v9 Drive folder (model in parts) ...')
        try:
            tree = drive_tree(v9_root()['models']['id']); pre = f'faster-whisper-{name}/'
            for f in info['files']: download(gdl(tree[pre + f]), os.path.join(dst, f), f'{name}/{f}')
            mn = pre + 'model.bin.manifest.json'; mp = os.path.join(dst, 'model.bin.manifest.json'); download(gdl(tree[mn]), mp, mn)
            man = load_json(mp); pdir = os.path.join(dst, 'parts'); os.makedirs(pdir, exist_ok=True)
            for p in man['parts']: download(gdl(tree[pre + p['name']]), os.path.join(pdir, p['name']), p['name'], p['size'], p.get('sha256'))
            if man['sha256'] != info['sha']: raise IOError('Drive model manifest does not match the known model')
            merge_parts(dst, man); shutil.rmtree(pdir, ignore_errors=True); shutil.rmtree(os.path.join(dst, 'work'), ignore_errors=True)
        except Exception as e: logging.error(traceback.format_exc()); say(f'   Drive did not work: {str(e)[:160]}', 'warning')
    if not complete():
        raise RuntimeError(f'could not download the {name} model from any source.\nCopy the folder faster-whisper-{name} (config.json, model.bin, tokenizer.json, vocabulary...) by hand into:\n   {MODELS}')
    say(f'   model {name} ready (SHA256 checked)'); return dst

# ---------- Stage 1 ----------
MODEL_CHOICES = {'1': ('medium', 'DEFAULT - good balance, safest on mid-range PCs'), '2': ('small', 'fastest, less accurate (very old / low-RAM PC)'), '3': ('large-v3', 'most accurate, heavy RAM/CPU — prefer medium if the PC restarts under load')}
def model_path(name):
    for d in (os.path.join(MODELS, 'faster-whisper-' + name), os.path.join(MODELS, name)):
        if os.path.isfile(os.path.join(d, 'model.bin')): return d
    return None
COMPUTE_ORDER = ['int8', 'int16', 'float32']
def get_model(name, compute=None):
    c = load_json(CFG, {}) or {}
    if c.get('hf_endpoint'): os.environ['HF_ENDPOINT'] = c['hf_endpoint']
    compute = compute or c.get('compute', {}).get(name, 'int8')
    from faster_whisper import WhisperModel
    p = model_path(name)
    threads = apply_resource_env(whisper_cpu_threads())
    if not p:
        say(f'   The {name} model is needed once (medium ~1.5 GB, small ~0.5 GB, large-v3 ~3 GB). Order: Hugging Face -> mirrors -> Karazma Drive -> manual copy.')
        p = fetch_model(name)
    # num_workers=1: never fan out parallel audio workers on top of cpu_threads (that multiplied load in older defaults)
    m = WhisperModel(p, device='cpu', compute_type=compute, cpu_threads=threads, num_workers=1)
    m.k_compute = compute; m.k_name = name; m.k_threads = threads; return m
def persian_ok(texts):
    """sanity check of a transcribed part: mostly Persian/Arabic letters (some CPUs give garbage with int8)"""
    t = ''.join(texts); fa = len(re.findall(r'[\u0600-\u06FF]', t)); lat = len(re.findall(r'[A-Za-z]', t))
    return not (lat >= 5 and 3 * lat > fa)
def remember_compute(name, compute):
    c = load_json(CFG, {}) or {}; c.setdefault('compute', {})[name] = compute; save_json(CFG, c)
def read_wav(p):
    import numpy as np
    with wave.open(p) as w: return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768, w.getframerate()
def chunk_bounds(x, sr, forced=(), target=480.0, search=30.0):
    """split points near every `target` s at the quietest 0.2 s window (so no word is cut), plus a split inside the
    silence after every clap (so the model never merges the broken take with the restart)"""
    import numpy as np
    n = len(x) / sr; pts = [0.0]; hop = int(0.05 * sr); forced = sorted(t for t in forced if 1.0 < t < n - 1.0)
    while True:
        nf = [t for t in forced if t > pts[-1] + 1.0]
        if nf and nf[0] <= pts[-1] + target + search: pts.append(round(nf[0], 2)); continue
        if n - pts[-1] <= target + search: break
        a, b = pts[-1] + target - search, pts[-1] + target + search
        seg = x[int(a * sr):int(b * sr)]; k = len(seg) // hop
        e = np.sqrt((seg[:k * hop].reshape(k, hop) ** 2).mean(1)); e = np.convolve(e, np.ones(4) / 4, 'same')
        pts.append(round(a + float(np.argmin(e)) * 0.05 + 0.1, 2))
    pts.append(round(n, 3)); return list(zip(pts[:-1], pts[1:]))
def stage1_episode(course, ep, model_name, get_m):
    lower_own_priority(); apply_resource_env(); say_resource_plan(model_name)
    d = ep_dir(course, ep); w = os.path.join(d, 'work'); os.makedirs(w, exist_ok=True)
    raw = raw_file(course, ep); st = load_json(st_path(course, ep), {}) or {}
    if st.get('model') and st['model'] != model_name and st.get('tr_done_chunks'):
        say(f'   note: this episode was started with {st["model"]}; continuing with {st["model"]} so the text stays consistent.'); model_name = st['model']
    st['model'] = model_name; st['raw'] = os.path.basename(raw); st['raw_size'] = os.path.getsize(raw); save_json(st_path(course, ep), st)
    info = probe(raw); dur = info['duration']; st['raw_info'] = info
    a16 = os.path.join(w, 'audio16k.wav'); a48 = os.path.join(w, 'audio48k_mono.wav')
    say('   1/5 extracting audio')
    for path, sr in ((a16, 16000), (a48, 48000)):
        if not (os.path.exists(path) and st.get('audio_ok', {}).get(str(sr)) == st['raw_size']):
            run([FFMPEG, '-v', 'error', '-y', '-i', raw, '-vn', '-ac', '1', '-ar', str(sr), '-c:a', 'pcm_s16le', path + '.tmp.wav'], 'audio extract')
            os.replace(path + '.tmp.wav', path); st.setdefault('audio_ok', {})[str(sr)] = st['raw_size']; save_json(st_path(course, ep), st)
    say('   2/5 finding claps')
    sys.path.insert(0, APP); import clap_detect as CD
    x48, sr48 = read_wav(a48); claps = CD.detect_claps(x48, sr48); del x48; import gc; gc.collect()
    say(f'       {len(claps)} clap(s) found')
    say(f'   3/5 transcribing with {model_name} (resumable: you can close the window and continue later)')
    x, sr = read_wav(a16); chunks = chunk_bounds(x, sr, [c['t'] + min(1.0, c.get('sil_s', 1.0) / 2) for c in claps]); st['tr_chunks'] = len(chunks); done = set(st.get('tr_done_chunks', []))
    jl = os.path.join(w, f'tr_{model_name}.jsonl')
    if not done and os.path.exists(jl): os.remove(jl)
    t0 = time.time()
    for i, (a, b) in enumerate(chunks):
        if i in done: continue
        while True:
            m = get_m(model_name); tc = time.time()
            segs, _ = m.transcribe(x[int(a * sr):int(b * sr)], language='fa', beam_size=5, word_timestamps=True, vad_filter=True,
                                   vad_parameters=dict(min_silence_duration_ms=300, speech_pad_ms=200), condition_on_previous_text=False)
            rows = []
            for s in segs:
                rows.append(dict(chunk=i, start=round(a + s.start, 3), end=round(a + s.end, 3), text=s.text.strip(),
                                 words=[dict(w=x_.word.strip(), s=round(a + x_.start, 3), e=round(a + x_.end, 3), p=round(x_.probability, 3)) for x_ in (s.words or [])]))
                print(f'\r       part {i + 1}/{len(chunks)}: {fmt(a + s.end)} of {fmt(dur)}   ', end='', flush=True)
            print()
            speechy = (b - a) > 5 and float((x[int(a * sr):int(b * sr)] ** 2).mean()) > 1e-5
            if persian_ok([r_['text'] for r_ in rows]) or not speechy or getattr(m, 'k_compute', '') == COMPUTE_ORDER[-1]:
                if speechy and rows: remember_compute(model_name, m.k_compute)
                st['compute'] = m.k_compute
                break
            nxt = COMPUTE_ORDER[COMPUTE_ORDER.index(m.k_compute) + 1]
            say(f'       text looks wrong with {m.k_compute} on this CPU; switching to {nxt} and redoing this part', 'warning')
            remember_compute(model_name, nxt); m = segs = None; import gc; gc.collect(); get_m(model_name, reset=True)
        with open(jl, 'a', encoding='utf-8') as f:
            for r_ in rows: f.write(json.dumps(r_, ensure_ascii=False) + '\n')
        done.add(i); st['tr_done_chunks'] = sorted(done); spd = (b - a) / max(0.1, time.time() - tc)
        st.setdefault('speed_x_realtime', {})[str(i)] = round(spd, 2); save_json(st_path(course, ep), st)
        say(f'       part {i + 1}/{len(chunks)} saved ({spd:.2f}x real time)')
    rows = {}
    for line in open(jl, encoding='utf-8'):
        if line.strip():
            r_ = json.loads(line); rows.setdefault(r_['chunk'], []).append(r_)
    segs = [s for i in sorted(rows) for s in rows[i]]
    words = [dict(i=k, **wd) for k, wd in enumerate(wd for s in segs for wd in s['words'] if wd['w'])]
    try: del x
    except NameError: pass
    import gc; gc.collect()
    say('   4/5 writing the -transcribe file')
    cuts = CD.clap_cuts(claps, [dict(w=q['w'], s=q['s'], e=q['e']) for q in words], max_back=8.0) if words else []
    write_transcribe(d, ep, course, raw, info, model_name + ' ' + st.get('compute', ''), segs, claps, cuts)
    save_json(os.path.join(d, ep + '.words.json'), words)
    save_json(os.path.join(d, ep + '.claps.json'), dict(params=CD.P, claps=claps, suggested_cuts=cuts))
    say('   5/5 frames for the head safe zone + package for Mosi')
    fr = []
    for k, t in enumerate((min(1.0, dur / 4), dur * 0.25, dur * 0.5, dur * 0.75)):
        fp = os.path.join(d, f'{ep}_frame{k + 1}_{t:.1f}s.jpg')
        run([FFMPEG, '-v', 'error', '-y', '-ss', f'{t:.2f}', '-i', raw, '-frames:v', '1', '-q:v', '3', fp], 'frame'); fr.append(fp)
    st.update(stage1='done', stage1_at=datetime.datetime.now().isoformat(timespec='seconds'), claps=len(claps), words=len(words), duration=dur)
    sp = list(st.get('speed_x_realtime', {}).values()); st['avg_speed_x_realtime'] = round(sum(sp) / len(sp), 2) if sp else None
    save_json(st_path(course, ep), st)
    for p in (a48,):
        try: os.remove(p)
        except OSError: pass
    say(f'   done: {len(words)} words, {len(claps)} claps')
def write_transcribe(d, ep, course, raw, info, model, segs, claps, cuts):
    dur = info['duration']; L = [f'# {ep} - {course}', f'# raw: {os.path.basename(raw)}  duration {fmt(dur)} ({dur:.2f}s)  {info["width"]}x{info["height"]} {info["fps"]:.2f}fps',
         f'# model: faster-whisper {model}   claps: {len(claps)}   created {datetime.datetime.now().isoformat(timespec="seconds")}',
         '# times are ELAPSED from the start of the raw file [mm:ss.s]. Remaining time = duration - elapsed.',
         '# [CLAP mm:ss.s] = teacher clapped here (stutter / wrong take). Word timings: ' + ep + '.words.json', '']
    ev = [(s['start'], 0, s) for s in segs] + [(c['t'], 1, c) for c in claps]
    # place each clap between the words it falls between (split the segment line there)
    out = []
    ci = sorted(claps, key=lambda c: c['t']); k = 0
    for s in segs:
        line = []; ls = None
        for wd in s['words'] or [dict(w=s['text'], s=s['start'], e=s['end'])]:
            while k < len(ci) and ci[k]['t'] <= wd['s']:
                if line: out.append(f'[{fmt(ls)}] ' + ' '.join(line)); line = []
                c = ci[k]; out.append(f'[CLAP {fmt(c["t"])}]   (silence after clap {c["sil_s"]:.1f}s, remaining {fmt(dur - c["t"])})'); k += 1
            if ls is None or not line: ls = wd['s']
            line.append(wd['w'])
        if line: out.append(f'[{fmt(ls)}] ' + ' '.join(line))
    while k < len(ci): c = ci[k]; out.append(f'[CLAP {fmt(c["t"])}]   (silence after clap {c["sil_s"]:.1f}s, remaining {fmt(dur - c["t"])})'); k += 1
    L += out
    if cuts:
        L += ['', '# ---- automatic suggestions (Mosi decides): broken sentence before each clap ----']
        L += [f'# suggest cut {fmt(a)} -> {fmt(b)}  {why}' for a, b, _, why in cuts]
    pz = pause_cuts([wd for s in segs for wd in (s['words'] or [])])
    if pz:
        L += ['', f'# ---- automatic pause cuts (v9.4): silence > {PAUSE_MAX_S:.1f}s between words ----']
        L += [f'# pause cut {fmt(a)} -> {fmt(b)}  ({b - a:.2f}s)' for a, b in pz]
    L += ['', '# ---- sentence cut rules (v9.4, unit = whole sentence, cuts only on word boundaries) ----',
          '# DROP: wrong takes / stutters (keep only the LAST complete take), incomplete sentences, repeated sentences (keep last),',
          f'#       fillers, unclear words, pauses > {PAUSE_MAX_S:.1f}s, "next episode" lines. KEEP: every complete teaching sentence.',
          f'# No kept piece shorter than {MIN_KEEP_S:.1f}s (back-to-back micro cuts are messy). Never cut inside a word.']
    with open(os.path.join(d, ep + '-transcribe.txt.tmp'), 'w', encoding='utf-8-sig', newline='\r\n') as f: f.write('\n'.join(L) + '\n')
    os.replace(os.path.join(d, ep + '-transcribe.txt.tmp'), os.path.join(d, ep + '-transcribe.txt'))

def pause_cuts(words, max_pause=None):
    """v9.4: cut ranges for silences longer than max_pause between consecutive words (keeps a short margin each side)."""
    mp = PAUSE_MAX_S if max_pause is None else max_pause; W = sorted((w for w in words if w.get('w')), key=lambda w: w['s']); out = []
    for a, b in zip(W, W[1:]):
        if b['s'] - a['e'] > mp:
            s, e = round(a['e'] + PAUSE_PAD[0], 3), round(b['s'] - PAUSE_PAD[1], 3)
            if e > s: out.append((s, e))
    return out

# ---------- Stage 2 ----------
FPS = 24
def plan_pieces(keep):
    """keep [[s,e],...] raw seconds -> [(s, frames)] at 24 fps (before speed-up). v9.4: pieces shorter than MIN_KEEP_S are dropped."""
    out = []
    for s, e in keep:
        n = int(round((float(e) - float(s)) * FPS))
        if n >= max(2, int(round(MIN_KEEP_S * FPS))): out.append((float(s), n))
    return out
def expected_cut_dur(keep, speed): return sum(n for _, n in plan_pieces(keep)) / FPS / speed
def check_cuts(V, dur):
    errs = []
    keep = V.get('keep')
    if not isinstance(keep, list) or not keep: return ['v9_cuts.keep is missing or empty']
    last = -1
    for i, k in enumerate(keep):
        if not (isinstance(k, list) and len(k) >= 2 and all(isinstance(v, (int, float)) for v in k[:2])): errs.append(f'keep[{i}] is not [start, end]'); continue
        s, e = k[:2]
        if s < 0 or e > dur + 0.05: errs.append(f'keep[{i}] {s}-{e} is outside the raw video (0-{dur:.2f})')
        if e - s < 0.1: errs.append(f'keep[{i}] {s}-{e} is shorter than 0.1 s')
        elif e - s < MIN_KEEP_S: say(f'   note: keep[{i}] {s}-{e} is shorter than {MIN_KEEP_S:.1f} s and will be dropped (no micro pieces)', 'warning')
        if s < last - 1e-6: errs.append(f'keep[{i}] starts before the previous one ends (must be sorted, no overlap)')
        last = e
    sp = V.get('speed', 1.1)
    if not isinstance(sp, (int, float)) or not 0.5 <= sp <= 2: errs.append('v9_cuts.speed must be between 0.5 and 2')
    return errs
def build_cut(course, ep, raw, V, work):
    pieces = plan_pieces(V['keep']); sp = float(V.get('speed', 1.1)); fade = float(V.get('fade_ms', 8)) / 1000
    pdir = os.path.join(work, 'pieces'); os.makedirs(pdir, exist_ok=True)
    sig = hashlib.sha256(json.dumps([pieces, sp, fade]).encode()).hexdigest()[:16]
    meta = load_json(os.path.join(pdir, 'pieces.json'), {}) or {}
    if meta.get('sig') != sig: shutil.rmtree(pdir, ignore_errors=True); os.makedirs(pdir); meta = {'sig': sig, 'done': []}
    for i, (s, n) in enumerate(pieces):
        f = os.path.join(pdir, f'p{i:04d}.mov'); bar(i, len(pieces), 'cutting')
        if i in meta['done'] and os.path.exists(f): continue
        D = n / FPS
        vf = 'scale=1920:1080:flags=lanczos,setsar=1,fps=24,format=yuv420p'
        af = f'aresample=48000,afade=t=in:d={fade},afade=t=out:st={max(0, D - fade):.4f}:d={fade},apad,atrim=end={D:.4f}'
        run([FFMPEG, '-v', 'error', '-y', '-ss', f'{s:.4f}', '-i', raw, '-t', f'{D + 0.2:.4f}', '-vf', vf, '-frames:v', str(n), '-af', af,
             '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '14', '-g', '24', '-c:a', 'pcm_s16le', '-ac', '2', '-ar', '48000', '-t', f'{D:.4f}', f + '.tmp.mov'], f'cut piece {i + 1}')
        os.replace(f + '.tmp.mov', f); meta['done'].append(i); save_json(os.path.join(pdir, 'pieces.json'), meta)
    bar(1, 1, 'cutting'); print()
    lst = os.path.join(pdir, 'list.txt')
    with open(lst, 'w', encoding='utf-8') as fh:
        for i in range(len(pieces)): fh.write(f"file 'p{i:04d}.mov'\n")
    fast = os.path.join(work, 'cut_fast.mov')
    if meta.get('fast') != sig or not os.path.exists(fast):
        say(f'   joining {len(pieces)} pieces and speeding up x{sp}')
        run([FFMPEG, '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', lst, '-filter_complex', f'[0:v]setpts=PTS/{sp},fps=24[v];[0:a]atempo={sp}[a]',
             '-map', '[v]', '-map', '[a]', '-c:v', 'libx264', '-preset', 'medium', '-crf', '16', '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', '-ar', '48000', fast + '.tmp.mov'], 'join + speed')
        os.replace(fast + '.tmp.mov', fast); meta['fast'] = sig; save_json(os.path.join(pdir, 'pieces.json'), meta)
    cut = os.path.join(work, ep + '_cut.mov')
    if meta.get('cut') != sig or not os.path.exists(cut):
        say('   speech loudness to -16 LUFS')
        _, err = run([FFMPEG, '-v', 'info', '-nostats', '-i', fast, '-vn', '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json', '-f', 'null', '-'], 'loudness measure')
        j = json.loads(err[err.rfind('{'):err.rfind('}') + 1])
        ln = f"loudnorm=I=-16:TP=-1.5:LRA=11:measured_I={j['input_i']}:measured_TP={j['input_tp']}:measured_LRA={j['input_lra']}:measured_thresh={j['input_thresh']}:offset={j['target_offset']}:linear=true,aresample=48000"
        run([FFMPEG, '-v', 'error', '-y', '-i', fast, '-map', '0:v', '-map', '0:a', '-c:v', 'copy', '-af', ln, '-c:a', 'pcm_s16le', '-ar', '48000', cut + '.tmp.mov'], 'loudness apply')
        os.replace(cut + '.tmp.mov', cut); meta['cut'] = sig; save_json(os.path.join(pdir, 'pieces.json'), meta)
    return cut
def renv():
    apply_resource_env()
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1', PYTHONUNBUFFERED='1')
    env['PATH'] = os.path.dirname(FFMPEG) + os.pathsep + env.get('PATH', ''); return env
def code(name):
    p = os.path.join(ASSETS, 'code', name); return p if os.path.exists(p) else os.path.join(APP, name)
def rcmd(*extra): return [sys.executable, code('render_episode.py'), *extra, '--assets', ASSETS, '--schema', code('edit_schema.json')]
def run_render(cmd, cwd):
    logging.info('render %s', cmd)
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=renv(), cwd=cwd, text=True, encoding='utf-8', errors='replace', creationflags=creation_flags())
    tail = []; t0 = time.time(); names = {'[1/4]': 'making graphics', '[2/4]': 'making music', '[3/4]': 'final render (longest step)', '[4/4]': 'preview'}
    try:
        for line in p.stdout:
            line = line.rstrip(); logging.info('[render] %s', line); tail = (tail + [line])[-25:]
            for k, v in names.items():
                if line.startswith(k): say(f'   {k} {v}...  (elapsed {int(time.time() - t0) // 60} min)')
        rc = p.wait()
    except BaseException: p.kill(); p.wait(); raise
    if rc != 0: raise RuntimeError('render failed:\n' + '\n'.join(tail))
def split_edit(edit_path, work):
    try: E = load_json(edit_path)
    except json.JSONDecodeError as e: raise ValueError(f'edit.json is broken (line {e.lineno}): {e.msg}')
    if not isinstance(E, dict): raise ValueError('edit.json is not a JSON object')
    V = E.pop('v9_cuts', None); rp = os.path.join(work, 'render.edit.json'); save_json(rp, E); return E, V, rp
def stage2_episode(course, ep):
    lower_own_priority(); apply_resource_env(); say_resource_plan('stage2')
    d = ep_dir(course, ep); w = os.path.join(d, 'work'); os.makedirs(w, exist_ok=True); st = load_json(st_path(course, ep), {}) or {}
    edit_path = os.path.join(d, ep + '.edit.json')
    why = skip_info(edit_path)
    if why is not None: say(f'   SKIPPED {ep} (Mosi marked it skip: {why}) - no cut/render.'); return 'SKIPPED'
    esha = sha256(edit_path); raw = raw_file(course, ep)
    if not raw: raise RuntimeError('raw video is missing in ' + d)
    E, V, rp = split_edit(edit_path, w); info = probe(raw)
    if V is None:   # v8-style log: the video in the folder is already cut by Mosi (sent in parts) -> graphics only
        say('   1/4 log without cuts: the video in this folder is used as the cut video (graphics only)'); cut = raw
        if E.get('mode') == 'final_passthrough':
            fdir = os.path.join(COURSES, course, FINAL_DIR); os.makedirs(fdir, exist_ok=True); out = os.path.join(fdir, ep + '.mp4')
            shutil.copyfile(raw, out + '.tmp'); os.replace(out + '.tmp', out); st.update(final_edit_sha=esha); save_json(st_path(course, ep), st); say(f'   DONE (already final): {out}'); return
    else:
        say('   1/4 checking the log'); errs = check_cuts(V, info['duration'])
        exp = expected_cut_dur(V['keep'], float(V.get('speed', 1.1))) if not errs else 0
        cv = (E.get('cut_video') or {}).get('duration')
        if not errs and isinstance(cv, (int, float)) and abs(cv - exp) > 0.25: errs.append(f'cut_video.duration {cv} but the keep list gives {exp:.3f} s')
        if errs: raise ValueError('\n'.join(errs))
        say(f'   2/4 cutting the raw video ({len(V["keep"])} pieces, about {fmt(exp)} after speed-up)'); cut = build_cut(course, ep, raw, V, w)
    r = subprocess.run(rcmd(cut, rp, '--validate-only'), capture_output=True, stdin=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace', env=renv(), creationflags=creation_flags())
    logging.info('validate %s %s', r.stdout, r.stderr)
    if r.returncode != 0: raise ValueError((r.stdout + r.stderr).strip()[-2000:])
    fdir = os.path.join(COURSES, course, FINAL_DIR); os.makedirs(fdir, exist_ok=True); out = os.path.join(fdir, ep + '.mp4')
    say('   3/4 graphics + final render (long step)'); rw = os.path.join(w, 'render_tmp'); shutil.rmtree(rw, ignore_errors=True)
    run_render(rcmd(cut, rp, '-o', out + '.r.mp4', '--work', rw), ASSETS)
    say('   4/4 checking the final video'); D = probe(out + '.r.mp4'); B = E['bumpers']
    want = min(E['cut_video']['duration'], probe(cut)['duration']) + B['dur'] * (len(B['insert_at']) + (1 if B.get('outro') else 0))
    bad = []
    if abs(D['duration'] - want) > max(0.5, 0.002 * want): bad.append(f'duration {D["duration"]:.2f}s, expected {want:.2f}s')
    if (D['width'], D['height']) != (E['output']['width'], E['output']['height']): bad.append(f'size {D["width"]}x{D["height"]}')
    if abs(D['fps'] - E['output']['fps']) > 0.05: bad.append(f'fps {D["fps"]:.3f}')
    if not D['audio']: bad.append('no sound')
    if bad: raise RuntimeError('final video check failed: ' + '; '.join(bad))
    os.replace(out + '.r.mp4', out); shutil.rmtree(rw, ignore_errors=True)
    st.update(final_edit_sha=esha, final_at=datetime.datetime.now().isoformat(timespec='seconds'), final=dict(D, sha256=sha256(out))); save_json(st_path(course, ep), st)
    say(f'   DONE: {out}  ({fmt(D["duration"])})')

# ---------- 4) upload Final videos to the host (FTPES) ----------
import ftplib, ssl, base64
def protect(s):
    if os.name != 'nt': return 'plain:' + base64.b64encode(s.encode()).decode()
    import ctypes, ctypes.wintypes as w
    class B(ctypes.Structure): _fields_ = [('n', w.DWORD), ('p', ctypes.POINTER(ctypes.c_char))]
    data = s.encode('utf-8'); i = B(len(data), ctypes.create_string_buffer(data, len(data))); o = B()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(i), None, None, None, None, 0, ctypes.byref(o)): raise OSError('DPAPI failed')
    r = ctypes.string_at(o.p, o.n); ctypes.windll.kernel32.LocalFree(o.p); return 'dpapi:' + base64.b64encode(r).decode()
def unprotect(s):
    kind, b = s.split(':', 1); raw = base64.b64decode(b)
    if kind == 'plain': return raw.decode()
    import ctypes, ctypes.wintypes as w
    class B(ctypes.Structure): _fields_ = [('n', w.DWORD), ('p', ctypes.POINTER(ctypes.c_char))]
    i = B(len(raw), ctypes.create_string_buffer(raw, len(raw))); o = B()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(i), None, None, None, None, 0, ctypes.byref(o)): raise OSError('saved password cannot be read on this Windows user/PC - enter it again')
    r = ctypes.string_at(o.p, o.n); ctypes.windll.kernel32.LocalFree(o.p); return r.decode('utf-8')
def read_password(prompt):
    if os.name == 'nt' and sys.stdin.isatty():
        try:
            import msvcrt
            sys.stdout.write(prompt); sys.stdout.flush(); buf = []
            while True:
                ch = msvcrt.getwch()
                if ch in ('\r', '\n'): sys.stdout.write('\n'); break
                if ch == '\x03': raise KeyboardInterrupt
                if ch in ('\x00', '\xe0'): msvcrt.getwch(); continue
                if ch == '\b':
                    if buf: buf.pop(); sys.stdout.write('\b \b'); sys.stdout.flush()
                    continue
                buf.append(ch); sys.stdout.write('*'); sys.stdout.flush()
            return ''.join(buf)
        except (ImportError, OSError): pass
    return read_line(prompt)
def host_cfg(force=False):
    c = load_json(CFG, {}) or {}; h = c.get('host') or {}
    if h.get('host') and h.get('user') and h.get('password') and not force: return c, h
    print('\n=== Host settings (asked once; the password is encrypted by Windows for this user) ===')
    hn = ask('Host (server name only)', h.get('host') or 'dl.karazma.co'); hn = re.sub(r'^[a-z]+://', '', hn, flags=re.I).split('/')[0].split('@')[-1]
    port = None
    if ':' in hn: hn, port = hn.split(':', 1)
    h['host'] = hn.strip().lower(); h['port'] = int(ask('Port', str(port or h.get('port') or 21)))
    h['user'] = ask('Username', h.get('user', 'karazma-team')) 
    pw = read_password('Password (Enter = keep the saved one): ' if h.get('password') else 'Password: ')
    if pw: h['password'] = protect(pw)
    while not h.get('password'):
        pw = read_password('Password: ')
        if pw: h['password'] = protect(pw)
    bp = ask('Folder on the host where course folders go (as in FileZilla "Remote site", e.g. /Long content)', '/' + h.get('base_path', '') if h.get('base_path') else '')
    h['base_path'] = bp.strip().strip('/'); h.setdefault('protocol', 'ftpes'); h.setdefault('allow_plain', False)
    c['host'] = h; save_json(CFG, c); return c, h
def ftp_open(h):
    f = ftplib.FTP(timeout=60) if h.get('allow_plain') else ftplib.FTP_TLS(timeout=60)
    f.encoding = 'utf-8'; f.connect(h['host'], int(h.get('port', 21))); f.login(h['user'], unprotect(h['password']))
    if not h.get('allow_plain'): f.prot_p()
    for seg in [x for x in h.get('base_path', '').split('/') if x]: f.cwd(seg)
    return f
def remote_names(f):
    try: return set(os.path.basename(n.rstrip('/')) for n in f.nlst())
    except ftplib.error_perm as e:
        if str(e).startswith('550'): return set()
        raise
def remote_size(f, name):
    try: f.voidcmd('TYPE I'); return f.size(name)
    except ftplib.error_perm: return None
def upload_menu():
    c = pick_course('Upload')
    if not c: return False
    finals = sorted(x for x in os.listdir(os.path.join(COURSES, c, FINAL_DIR)) if x.lower().endswith('.mp4')) if os.path.isdir(os.path.join(COURSES, c, FINAL_DIR)) else []
    if not finals: say(f'No final video in {os.path.join(COURSES, c, FINAL_DIR)} yet. Run 3 (Edit) first.'); return False
    cfg, h = host_cfg()
    try: f = ftp_open(h)
    except Exception as e:
        say(f'Could not log in to the host: {e}', 'error')
        if yes('Enter the host settings again?', 'y'): cfg, h = host_cfg(True)
        return False
    ok = 0
    with f:
        if c not in remote_names(f):
            # asked EVERY session, never remembered
            if not yes(f'Course folder "{c}" does NOT exist on the host. Create it and upload?', 'n'): say('Skipped this course (you will be asked again next time).'); return False
            f.mkd(c)
        f.cwd(c); have = remote_names(f)
        todo = [x for x in finals if x not in have or remote_size(f, x) != os.path.getsize(os.path.join(COURSES, c, FINAL_DIR, x))]
        if not todo: say('Everything in Final is already on the host. Nothing to upload.'); return False
        say(f'{len(todo)} file(s) to upload: ' + ', '.join(todo))
        for i, x in enumerate(todo, 1):
            p = os.path.join(COURSES, c, FINAL_DIR, x); total = os.path.getsize(p); sent = [0]
            def cb(b): sent[0] += len(b); bar(sent[0], total, f'[{i}/{len(todo)}] {x}')
            try:
                with open(p, 'rb') as fh: f.storbinary('STOR ' + x + '.uploading', fh, 1 << 20, cb)
                print()
                if remote_size(f, x + '.uploading') != total: raise IOError('size on host is different after upload')
                if x in have:
                    try: f.delete(x)
                    except ftplib.error_perm: pass
                f.rename(x + '.uploading', x); ok += 1; say(f'   uploaded {x}')
            except Exception as e: print(); say(f'   ERROR uploading {x}: {e}', 'error')
    web = h.get('web_base') or f'https://{h["host"]}/' + '/'.join(urllib.parse.quote(s) for s in [h.get('user', '')] + [x for x in h.get('base_path', '').split('/') if x] + [c])
    say(f'\nUpload finished: {ok} of {len(todo)} OK.  Links start with: {web}/'); return False

# ---------- menus ----------
def pick_course(title):
    cs = courses()
    if not cs: say('No course yet. Choose 1 first (download a course).'); return None
    print(f'\n{title} - choose ONE course:')
    for i, c in enumerate(cs, 1):
        eps = episodes(c); s = [ep_status(c, e)[0] for e in eps]
        print(f'  [{i}] {c}   ({len(eps)} episodes: ' + ', '.join(f'{s.count(k)} {n}' for k, n in (('raw', 'to transcribe'), ('sent', 'waiting for log'), ('log', 'log ready'), ('done', 'final'), ('skip', 'skipped')) if s.count(k)) + ')')
    print('  [0] back')
    k = ask('Course number', '1' if len(cs) == 1 else '')
    return cs[int(k) - 1] if k.isdigit() and 1 <= int(k) <= len(cs) else None
def finish_question(): return yes('Close this program automatically when everything is finished? (good for overnight)', 'n')
def run_batch(course, eps, fn, title):
    t0 = time.time(); res = []
    for i, ep in enumerate(eps, 1):
        say(f'\n=== {title} [{i}/{len(eps)}] {course} | {ep} ===')
        try: r = fn(course, ep); res.append((ep, 'SKIPPED' if r == 'SKIPPED' else 'OK'))
        except ValueError as e: say(f'ERROR in the edit log of {ep}:\n{e}\nSend this message to Mosi.', 'error'); res.append((ep, 'LOG ERROR - send the message to Mosi'))
        except Exception as e:
            logging.error(traceback.format_exc()); say(f'ERROR for {ep}: {e}\nDetails: {LOG}', 'error'); res.append((ep, 'ERROR'))
    say(f'\n===== {title} finished in {int(time.time() - t0) // 60} min =====')
    for ep, r in res: say(f'   {ep:<10} {r}')
    return res
def course_bundle(course):
    """one zip per course for Mosi: every transcribed episode's -transcribe.txt, words.json, claps.json and first frame"""
    cdir = os.path.join(COURSES, course); zp = os.path.join(cdir, safe_name(course) + '_for_Mosi.zip'); n = 0
    with zipfile.ZipFile(zp + '.tmp', 'w', zipfile.ZIP_DEFLATED) as z:
        for ep in episodes(course):
            d = ep_dir(course, ep)
            if not os.path.exists(os.path.join(d, ep + '-transcribe.txt')): continue
            for f in [ep + '-transcribe.txt', ep + '.words.json', ep + '.claps.json'] + sorted(x for x in os.listdir(d) if x.startswith(ep + '_frame1_')):
                if os.path.exists(os.path.join(d, f)): z.write(os.path.join(d, f), f'{ep}/{f}')
            n += 1
    os.replace(zp + '.tmp', zp)
    for old in [x for x in os.listdir(cdir) if re.match(r'^\d+_' + re.escape(os.path.basename(zp)), x)] + ([os.path.basename(zp) + '.manifest.json'] if os.path.exists(zp + '.manifest.json') else []): os.remove(os.path.join(cdir, old))
    if os.path.getsize(zp) > (95 << 20): split_parts(zp); say('   (the zip is over 95 MB, so it was also split into parts NN_... + .manifest.json; send all of them)')
    return zp, n
def transcribe_menu():
    need_ff()
    try: import faster_whisper, numpy  # noqa
    except Exception as e: raise RuntimeError(f'speech engine missing ({e}). Extract ALL THREE karazma-studio-v9_*.zip files into the same folder, then try again.')
    c = pick_course('Transcribe + find claps')
    if not c: return False
    merge_local_parts(c)
    eps = [e for e in episodes(c) if ep_status(c, e)[0] == 'raw']
    if not eps:
        done = [e for e in episodes(c) if ep_status(c, e)[0] in ('sent', 'log', 'done', 'skip')]
        say('Nothing to transcribe in this course' + (f' (already done: {", ".join(done)}).' if done else ' (no raw videos - choose 1 first).'))
        if done: zp, n = course_bundle(c); say(f'Bundle for Mosi (re-made): {zp}')
        return False
    say(f'{len(eps)} episode(s) will be transcribed in order: {", ".join(eps)}')
    print('Transcription model:')
    for k, (n, why) in MODEL_CHOICES.items(): print(f'  [{k}] {n:<9} {why}' + ('   (already downloaded)' if model_path(n) else ''))
    cfg = load_json(CFG, {}) or {}; k = ask('Model (Enter = medium)', cfg.get('model_key', '1'))
    mname = MODEL_CHOICES.get(k, MODEL_CHOICES['1'])[0]; cfg = load_json(CFG, {}) or {}; cfg['model_key'] = k if k in MODEL_CHOICES else '1'; save_json(CFG, cfg)
    warn_model_ram(mname); lower_own_priority(); apply_resource_env(); say_resource_plan(mname)
    auto = finish_question(); cache = {}
    def get_m(n, reset=False):
        if reset or n not in cache: cache.clear(); say(f'   loading model {n}...'); cache[n] = get_model(n)
        return cache[n]
    run_batch(c, eps, lambda co, ep: stage1_episode(co, ep, mname, get_m), 'Transcribe')
    cache.clear(); zp, n = course_bundle(c)
    say(f'\nSaved. Send THIS file to Mosi (it has all {n} transcribed episodes of the course):\n   {zp}\nYou can close the window now; nothing is lost.')
    return auto
def edit_menu():
    need_ff(); c = pick_course('Edit (apply Mosi\'s logs)')
    if not c: return False
    import_logs(c); merge_local_parts(c)
    sk = [(e, ep_status(c, e)[1]) for e in episodes(c) if ep_status(c, e)[0] == 'skip']
    for e, m in sk: say(f'   {e}: {m} - will not be cut/rendered.')
    eps = [e for e in episodes(c) if ep_status(c, e)[0] == 'log']
    if not eps:
        if sk: say('Nothing else to edit (all remaining episodes are marked SKIP).'); return False
        say(f'No edit log found for "{c}".\nPut Mosi\'s <Course>_logs.zip (or the <EP>.edit.json files) in:\n   {os.path.join(COURSES, c)}\nand choose 3 again.'); return False
    say(f'{len(eps)} episode(s) with a log: {", ".join(eps)}'); auto = finish_question()
    run_batch(c, eps, stage2_episode, 'Edit')
    say(f'\nFinal videos are in: {os.path.join(COURSES, c, FINAL_DIR)}\nWhen all are there, choose 4 to upload.'); return auto
def download_menu():
    print('\nDownload from:\n  [a] the host (paste the message with the links)\n  [b] Google Drive (paste the folder link)\n  [c] continue unfinished host downloads\n  [0] back')
    k = ask('Choose a, b, c or 0', 'a').lower()
    if k in ('a', '1'): host_course()
    elif k in ('b', '2'): drive_course()
    elif k in ('c', '3'): download_pending()
    return False
def check_for_update():
    """v9.6: compare local version with app_manifest.json on GitHub (public raw), then download changed app files."""
    say(f'Checking for update (this version: {VERSION}; source: GitHub {GH_OWNER}/{GH_REPO}@{GH_BRANCH}/{GH_APP_DIR}) ...')
    try:
        mp = os.path.join(APP, 'app_manifest.check.json')
        download(github_raw_url('app_manifest.json'), mp, 'app_manifest.json')
        remote = str((load_json(mp, {}) or {}).get('version', '?')); os.remove(mp)
    except Exception as e:
        logging.error(traceback.format_exc()); say(f'Could not check for update: {e}', 'warning'); return
    if remote == VERSION: say(f'You have the latest version ({VERSION}). Files will still be verified.')
    else: say(f'New version on GitHub: {remote} (you have {VERSION}). Downloading ...')
    if sync_from_drive() or remote != VERSION: restart_program([])
def menu():
    while True:
        print(f'\n======  KARAZMA STUDIO v{VERSION}  (SOP 2.5)  ======')
        print('   v9.1+: CPU/RAM throttling on (leaves cores free so Windows stays stable)')
        print('   v9.2+: episodes Mosi marks "skip" in the logs are skipped in option 3')
        print('   v9.6: [5] Check for update from GitHub  |  auto-restart after an update')
        print(' [1] Download a course (host links or Google Drive folder)')
        print(' [2] Transcribe + find claps  ->  one zip for Mosi')
        print(' [3] Edit: apply Mosi\'s logs  ->  Final folder')
        print(' [4] Upload a course (Final videos) to the host')
        print(' [5] Check for update')
        print(' [0] Exit')
        print(f'   Courses folder: {COURSES}')
        k = read_line('Type a number and press Enter: ').strip()
        auto = False
        try:
            if k == '1': auto = download_menu()
            elif k == '2': auto = transcribe_menu()
            elif k == '3': auto = edit_menu()
            elif k == '4': auto = upload_menu()
            elif k == '5': check_for_update()
            elif k in ('0', 'q', 'exit', ''): return 0
            else: print('Please type 0-5.')
        except (RuntimeError, OSError, IOError, ftplib.Error) as e:
            logging.error(traceback.format_exc()); say(f'ERROR: {e}', 'error')
        if auto: say('Finished; closing automatically.'); return 0
def show_status():
    for c in courses():
        print(f'Course: {c}')
        for ep in episodes(c): print(f'   {ep:<10} {ep_status(c, ep)[1]}')
def selftest():
    ok = True
    def chk(name, fn):
        nonlocal ok
        try: r = fn(); print(f'  [ok]   {name} {r or ""}')
        except Exception as e: ok = False; print(f'  [FAIL] {name}: {type(e).__name__}: {e}'); logging.error(traceback.format_exc())
    print(f'Karazma Studio v{VERSION} self-test   python {sys.version.split()[0]}   app: {APP}')
    chk('ffmpeg', lambda: (need_ff(), run([FFMPEG, '-version'], 'ffmpeg')[0].decode()[:40])[1])
    chk('numpy', lambda: __import__('numpy').__version__)
    chk('Pillow + raqm (Persian text)', lambda: (lambda f: f.check('raqm') or (_ for _ in ()).throw(RuntimeError('raqm missing')))(__import__('PIL.features', fromlist=['x'])))
    chk('ctranslate2', lambda: __import__('ctranslate2').__version__ + ' ' + str(__import__('ctranslate2').get_supported_compute_types('cpu')))
    chk('onnxruntime', lambda: __import__('onnxruntime').__version__)
    chk('av', lambda: __import__('av').__version__)
    chk('faster_whisper', lambda: __import__('faster_whisper').__version__)
    def clap():
        import numpy as np; sys.path.insert(0, APP); import clap_detect as CD
        sr = 48000; x = np.zeros(4 * sr, np.float32); n = int(0.04 * sr); b = np.random.default_rng(1).normal(0, 1, n) * np.exp(-np.arange(n) / (0.006 * sr))
        x[sr:sr + n] = 0.9 * b / abs(b).max(); c = CD.detect_claps(x, sr)
        if len(c) != 1: raise RuntimeError(f'{len(c)} claps found, expected 1')
        return f'clap at {c[0]["t"]:.2f}s'
    chk('clap detector', clap)
    chk('renderer assets', lambda: len(load_json(os.path.join(ASSETS, 'assets_manifest.json'))['assets']))
    print('SELF-TEST PASSED' if ok else 'SELF-TEST FAILED - send this screen to Mosi'); return 0 if ok else 1
def cli(a):
    """non-interactive (tests / Task Scheduler): --status | --message FILE | --drive LINK | --transcribe COURSE [--model small] | --edit COURSE"""
    if '--sync' in a and len(a) == 1: return 0
    if '--fetch-model' in a: print(fetch_model(a[a.index('--fetch-model') + 1])); return 0
    if '--status' in a: show_status(); return 0
    if '--selftest' in a: return selftest()
    if '--parse' in a: print(json.dumps(parse_message(open(a[a.index('--parse') + 1], encoding='utf-8-sig').read()), ensure_ascii=False)); return 0
    if '--message' in a: return 0 if host_course(open(a[a.index('--message') + 1], encoding='utf-8-sig').read()) else 1
    if '--drive' in a: return 0 if drive_course(a[a.index('--drive') + 1]) else 1
    need_ff()
    if '--transcribe' in a:
        c = a[a.index('--transcribe') + 1]; model = a[a.index('--model') + 1] if '--model' in a else 'medium'; cache = {}
        def gm(n, reset=False):
            if reset or n not in cache: cache.clear(); cache[n] = get_model(n)
            return cache[n]
        merge_local_parts(c); eps = [e for e in episodes(c) if ep_status(c, e)[0] == 'raw']
        r = run_batch(c, eps, lambda co, ep: stage1_episode(co, ep, model, gm), 'Transcribe'); print(course_bundle(c)[0])
        return 0 if all(x[1] == 'OK' for x in r) else 1
    if '--edit' in a:
        c = a[a.index('--edit') + 1]; import_logs(c); merge_local_parts(c)
        r = run_batch(c, [e for e in episodes(c) if ep_status(c, e)[0] == 'log'], stage2_episode, 'Edit'); return 0 if all(x[1] in ('OK', 'SKIPPED') for x in r) else 1
    return 2

if __name__ == '__main__':
    a = sys.argv[1:]; rc = 0
    try:
        if (not a or '--sync' in a) and sync_from_drive(): restart_program(a)
        rc = cli(a) if a else menu()
    except KeyboardInterrupt: print('\nStopped. Everything finished so far is saved; run again to continue.'); rc = 0
    except EOFError: rc = 0
    except Exception as e:
        logging.error(traceback.format_exc()); print(f'\nUNEXPECTED ERROR: {e}\nDetails: {LOG}'); rc = 1
    sys.exit(rc)
