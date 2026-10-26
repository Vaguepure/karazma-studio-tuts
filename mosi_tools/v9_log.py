# Mosi-side (box only): plan.json (my decisions on the RAW timeline) + <EP>_for_Mosi files -> <EP>.edit.json (SOP 2.5: render log + v9_cuts)
# usage: python v9_log.py PLAN.json FOR_MOSI_DIR OUT_EDIT.json   (FOR_MOSI_DIR = unzipped <EP>_for_Mosi.zip)
# SUGGEST (v9.4): python v9_log.py --suggest FOR_MOSI_DIR C####  -> auto pause cuts (>1 s) + filler/repeat/unclear suggestions
# v9.4: pauses >1 s between words are cut automatically (plan "auto_pause_cuts": false to disable); kept pieces <1 s are dropped.
# SKIP (v9.2): python v9_log.py --skip "02 - C5030" "reason" OUT_DIR   -> OUT_DIR/02 - C5030.edit.json = {"episode":..,"skip":true,"reason":..}
#   or put "skip": true (+ "reason") in plan.json -> same stub, no transcript needed. studio9 v9.2 option 3 skips it (no cut/render).
# plan.json:
#  {"episode":"C4600","course_label":"...", "trim_head":1.2, "trim_tail":null|sec,
#   "cut_raw":[[s,e,"why"],...],                     # removed ranges, RAW seconds (claps/stutters/retakes/outro "next episode")
#   "hook":{"template":"T1","parts":[["متن",false],["کلیدی",true]],"at_raw":2.0,"dur":4.0,"side":"R","cy":540},
#   "headings":[{"id":"H1","parts":[[..]],"topic_word_raw":63.2,"dur":3.4}],   # start = cut(topic first word) + 0.85 s (+1 s delay rule)
#   "cards":[{"id":"K1","text":"...","at_raw":80.0,"dur":3.8,"side":"L"}],
#   "zooms":[{"at_raw":40.0,"dur":4.0}], "logo_raw":[30.0, ...], "zoom_anchor":[0.56,0.37]}
import sys, os, json, glob, subprocess, numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); APPD = os.environ.get('V9_APP', os.path.join(HERE, '..', 'app'))
sys.path.insert(0, APPD); import render_episode as R
R.set_assets(os.path.join(APPD, 'assets'))
SPEED, FPS = 1.1, 24
RULES = dict(max_no_event=30.0, min_gap=15.0, close_warn=10.0, first_heading=(5.0, 15.0), heading_after_word=0.85,
             bumper_every=300.0, bumper_pause=0.15, last_bumper_to_outro=60.0, zoom_per_min=3, zoom_scale=1.12,
             logo_after_heading=10.0, bumper_before_heading=10.0, bumper_after_heading=20.0,
             pause_max=1.0, min_keep=1.0, pause_pad=(0.10, 0.14))  # v9.4 Arsha 2026-10-26: pause >1 s cut, no keep <1 s  # Arsha 2026-10-26: logo/bumper never clash with heading
FILLERS = {'خب', 'خوب', 'اوم', 'ام', 'اِم', 'اه', 'آه', 'ممم', 'یعنی', 'چیز', 'اینکه'}
def pause_cuts(words, mp=None):
    """v9.4: [s,e,'pause'] for silences > pause_max between words (margins kept, never cuts a word)."""
    mp = RULES['pause_max'] if mp is None else mp; W = sorted((w for w in words if w.get('w')), key=lambda w: w['s']); out = []
    for a, b in zip(W, W[1:]):
        if b['s'] - a['e'] > mp:
            s_, e_ = round(a['e'] + RULES['pause_pad'][0], 3), round(b['s'] - RULES['pause_pad'][1], 3)
            if e_ > s_: out.append([s_, e_, f'pause {b["s"] - a["e"]:.2f}s'])
    return out
def suggest_cuts(words, sent_gap=0.35):
    """v9.4 sentence rules -> SUGGESTIONS for Mosi (review, then copy into cut_raw): fillers as whole-word cuts,
    repeated sentence starts (drop earlier take, keep LAST), low-confidence words flagged. Unit = sentence; word boundaries only."""
    W = sorted((w for w in words if w.get('w')), key=lambda w: w['s']); sug = []; sents = []; cur = []
    for w in W:
        if cur and w['s'] - cur[-1]['e'] >= sent_gap: sents.append(cur); cur = []
        cur.append(w)
    if cur: sents.append(cur)
    for i, w in enumerate(W):
        if w['w'].strip('.,،؟!') in FILLERS:
            pv = W[i - 1]['e'] if i else w['s']; nx = W[i + 1]['s'] if i + 1 < len(W) else w['e']
            sug.append([round(max(pv, w['s'] - 0.05), 3), round(min(nx, w['e'] + 0.05), 3), f'filler "{w["w"]}" (check)'])
        elif w.get('p', 1) < 0.35: sug.append([w['s'], w['e'], f'unclear word "{w["w"]}" p={w["p"]} (check, do not guess)'])
    key = lambda S: ' '.join(x['w'] for x in S[:3])
    for a, b in zip(sents, sents[1:]):
        if len(a) >= 2 and key(a)[:12] == key(b)[:12]:
            sug.append([a[0]['s'], b[0]['s'], 'repeat/retake: earlier take dropped, keep last'])
    return sorted(sug)
def keep_from(plan, dur, words=None):
    a = float(plan.get('trim_head') or 0); b = float(plan.get('trim_tail') or dur)
    extra = pause_cuts(words) if (words and plan.get('auto_pause_cuts', True)) else []
    cuts = sorted([c[0], c[1]] for c in plan.get('cut_raw', []) + extra)
    keep = []; cur = a
    for s, e in cuts:
        if e <= cur: continue
        if s > cur: keep.append([round(cur, 3), round(min(s, b), 3)])
        cur = max(cur, e)
        if cur >= b: break
    if cur < b: keep.append([round(cur, 3), round(b, 3)])
    # v9.4: no kept piece shorter than min_keep (studio9 v9.4 drops them too, so the timelines match)
    return [k for k in keep if round((k[1] - k[0]) * FPS) >= max(2, int(round(RULES['min_keep'] * FPS)))]
def mapper(keep):
    P = []; acc = 0
    for s, e in keep: n = int(round((e - s) * FPS)); P.append((s, s + n / FPS, acc)); acc += n / FPS
    total = acc / SPEED
    def f(t):
        for s, e, a0 in P:
            if t < s: return a0 / SPEED          # inside a removed range -> start of next kept piece
            if t <= e: return (a0 + t - s) / SPEED
        return total
    return f, total
def write_skip(ep_name, reason, out_p):
    if os.path.isdir(out_p): out_p = os.path.join(out_p, ep_name + '.edit.json')
    json.dump(dict(episode=ep_name, skip=True, reason=reason or 'skipped by Mosi'), open(out_p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('SKIP stub written:', out_p); return 0
def main(plan_p, d, out_p):
    plan = json.load(open(plan_p, encoding='utf-8')); EP = plan['episode']
    if plan.get('skip'): return write_skip(EP, plan.get('reason'), out_p)
    import re
    _m = re.search(r'(C\d{4})', str(EP));
    if _m: EP = _m.group(1); plan['episode'] = EP
    elif not re.fullmatch(r'C\d{4}', str(EP)):
        raise SystemExit(f'episode must be C####, got {EP!r}')
    words = json.load(open(os.path.join(d, EP + '.words.json'), encoding='utf-8-sig'))
    hdr = open(os.path.join(d, EP + '-transcribe.txt'), encoding='utf-8-sig').read()
    import re; dur = float(re.search(r'\(([\d.]+)s\)', hdr).group(1)); raw_name = re.search(r'# raw: (\S+)', hdr).group(1)
    keep = keep_from(plan, dur, words); cut, D = mapper(keep); D = round(D, 3)
    kw = [w for w in words if any(s <= w['s'] and w['e'] <= e + 0.02 for s, e in keep)]
    ends = [cut(w['e']) for w, nx in zip(kw, kw[1:] + [None]) if nx is None or cut(nx['s']) - cut(w['e']) >= 0.3]   # sentence ends (cut time)
    def parts(p): return [dict(text=t, accent=bool(k)) for t, k in p]
    H = plan['hook']
    E = dict(schema_version='1.1', episode=EP, course_label=plan['course_label'], brand_label='کارازما',
             cut_video=dict(file=EP + '_cut.mov', duration=D, fps=24, width=1920, height=1080, speed_applied=SPEED, speech_lufs=-16.0),
             output=dict(width=1920, height=1080, fps=24, vcodec='libx264', preset='slow', crf=21, pix_fmt='yuv420p', acodec='aac', abitrate='160k', arate=48000, target_lufs=-16.0, true_peak_limit=0.89, preview_480p=False),
             hook=dict(template=H.get('template', 'T1'), text=' '.join(t for t, _ in H['parts']), parts=parts(H['parts']), start=round(cut(H['at_raw']), 3), dur=H.get('dur', 4.0), side=H.get('side', 'R'), cy=H.get('cy', 540)),
             headings=[], cards=[], splits=[], zooms=[], sfx=[])
    for h in plan.get('headings', []):
        st = round(cut(h['topic_word_raw']) + RULES['heading_after_word'] + 1.0, 3)   # Arsha 2026-10-15: headings 1 s later than before
        E['headings'].append(dict(id=h['id'], text=' '.join(t for t, _ in h['parts']), parts=parts(h['parts']), start=st, dur=h.get('dur', 3.4)))
        E['sfx'].append({'type': 'whoosh', 'file': 'sfx.whoosh_v2', 'time': st, 'offset_s': -0.3, 'gain_db': 0.0, 'for': h['id']})
    for c in plan.get('cards', []):
        fb = R.font('Bold', 44); lines = R._balance(fb, c['text'], 420); body, _, _ = R.card_layers_v2(c['text'], lines)
        CW, CH = body.width + 60, body.height + 60; cx = 1560 if c.get('side', 'R') == 'R' else 310; cy = 540; ox, oy = cx - CW // 2, cy - CH // 2
        # Map RAW transcript time -> cut timeline. Do NOT cascade-delay for min-gap
        # (that caused 30–90s late cards). If a full-screen heading covers the same
        # instant, start the card just after that heading ends (~0.35s), so sync to
        # speech stays within a few seconds, never a minute.
        st = cut(c['at_raw'])
        for h in E['headings']:
            hs, he = h['start'], h['start'] + h['dur']
            if hs - 0.05 <= st < he:
                st = he + 0.35
        st = round(st, 3)
        E['cards'].append(dict(id=c['id'], text=c['text'], lines=lines, start=st, dur=c.get('dur', 3.8), side=c.get('side', 'R'), cx=cx, cy=cy,
                               box=[ox + 70, oy + 70, ox + 30 + body.width - 40, oy + 30 + body.height - 40], key_type=c.get('key_type', 'important')))
        E['sfx'].append({'type': 'msg', 'file': 'sfx.msg', 'time': st, 'offset_s': 0.04, 'gain_db': 0.0, 'for': c['id']})
    for z in plan.get('zooms', []):
        s = round(cut(z['at_raw']), 3); E['zooms'].append(dict(start=s, end=round(min(D, s + z.get('dur', 4.0)), 3), scale=RULES['zoom_scale'], anchor=plan.get('zoom_anchor', [0.56, 0.37]), ease_s=0.45, reason=z.get('why', 'key sentence')))
    LMJ = json.load(open(R.A('logo.motion_meta')))
    # Headings are placed first. Logomotion must NEVER overlap a heading (Arsha 2026-10-26).
    # If a logo would coincide with a heading window, keep the heading and start logo
    # logo_after_heading seconds AFTER that heading ends. Cards use cut(at_raw) only —
    # never shift them; at_raw must be the transcript phrase moment on the RAW timeline.
    logo_times = []
    for t_raw in plan.get('logo_raw', []):
        t = cut(t_raw)
        for _ in range(len(E['headings']) + 2):
            moved = False
            for h in E['headings']:
                hs, he = h['start'], h['start'] + h['dur']
                if not (t + LMJ['dur'] <= hs + 1e-6 or t >= he - 1e-6):
                    t = he + RULES['logo_after_heading']
                    moved = True
                    break
            if not moved:
                break
        if t + LMJ['dur'] <= D + 0.05:
            logo_times.append(round(t, 3))
    # de-dupe logos that collapsed onto the same slot after heading pushes
    dedup = []
    for t in sorted(logo_times):
        if not dedup or t - dedup[-1] >= 12.0:
            dedup.append(t)
    E['logo_motion'] = dict(file='logo.motion', corner='top-right', x=LMJ['x'], y=LMJ['y'], size_px=LMJ.get('size', 88), dur=LMJ['dur'], times=dedup, static_logo=False)
    # bumpers: every ~5 min of cut time, snapped to the NEXT sentence end + 0.15 s; none closer than 60 s to the outro
    # Headings are placed first (Arsha 2026-10-26). Adjust bumper vs heading:
    #   if bumper < 10s before a heading start -> move to exactly 10s before that heading
    #   if bumper < 20s after a heading start  -> move to exactly 20s after that heading
    bt = []; nxt = RULES['bumper_every']
    while nxt < D - RULES['last_bumper_to_outro']:
        e = next((x for x in ends if x >= nxt), None)
        if e is None or e + RULES['bumper_pause'] > D - RULES['last_bumper_to_outro']: break
        bt.append(round(e + RULES['bumper_pause'], 3)); nxt = bt[-1] + RULES['bumper_every']
    def _adjust_bumper(t):
        for _ in range(len(E['headings']) + 3):
            moved = False
            for h in E['headings']:
                hs = h['start']
                if hs - 10.0 < t < hs:          # too close before heading
                    t = hs - 10.0; moved = True; break
                if hs <= t < hs + 20.0:         # too close after heading start
                    t = hs + 20.0; moved = True; break
            if not moved:
                break
        return t
    bt2 = []
    for t in bt:
        t2 = round(_adjust_bumper(t), 3)
        if t2 < 0 or t2 > D - RULES['last_bumper_to_outro']:
            continue
        if bt2 and t2 - bt2[-1] < 60.0:  # keep bumpers reasonably spaced after pushes
            continue
        bt2.append(t2)
    bt = bt2
    E['bumpers'] = dict(file='bumper.main', dur=6.0, insert_at=bt, outro=True, gain_db=-8.0, reason='every ~5 min, after sentence + 0.15 s; heading priority: >=10s before or >=20s after heading start')
    E['music'] = dict(track_id='music.gymnopedie1', loudnorm_lufs=-30.9, gain=1.0, duck=dict(threshold_db=-30.0, ratio=2, attack_ms=50, release_ms=500, approx_duck_db=-6.0), crossfade_s=3.0, fade_in_s=2.0, fade_out_s=3.0, bumper_dip=True)
    E['music_credit'] = 'Gymnopedie No 1 – Kevin MacLeod (incompetech.com), CC BY 4.0'
    # head safe zone from the first frame jpg (YuNet)
    import cv2
    fr = sorted(glob.glob(os.path.join(d, EP + '_frame1_*.jpg')))[0]; img = cv2.imread(fr); h0, w0 = img.shape[:2]; sm = cv2.resize(img, (960, 540))
    yunet = os.environ.get('YUNET', '/workspace/KARAZMA_SHARED_KEEP/models/yunet.onnx')
    ff = None
    for thr in (0.6, 0.45, 0.3):
        det = cv2.FaceDetectorYN.create(yunet, '', (960, 540), thr); _, ff = det.detect(sm)
        if ff is not None:
            big = [q for q in ff if q[2]*q[3] > 960*540*0.01]
            if big: ff = big; break
            ff = None
    if plan.get('head_safe_zone'):
        E['head_safe_zone'] = plan['head_safe_zone']
    elif ff is None:
        E['head_safe_zone'] = dict(box=[525, 0, 1235, 1080], source='fallback center talking-head (empty frame1)', face_box_first_frame=[762, 106, 998, 423], margins=dict(side_w=1.0, top_h=1.0, bottom_h=2.2))
    else:
        x, y, w, h = [v * 1920 / 960 for v in max(ff, key=lambda q: q[2] * q[3])[:4]]
        E['head_safe_zone'] = dict(box=[int(max(0, x - w)), int(max(0, y - h)), int(min(1920, x + 2 * w)), int(min(1080, y + h + 2.2 * h))], source=f'YuNet on {os.path.basename(fr)} (raw {w0}x{h0}), margins 1w sides, 1h top, 2.2h bottom',
                                 face_box_first_frame=[int(x), int(y), int(x + w), int(y + h)], margins=dict(side_w=1.0, top_h=1.0, bottom_h=2.2))
    hfn = {'T1': R.hook_t1_frames, 'T2': R.hook_t2_frames, 'T3': R.hook_t3_frames}[E['hook']['template']]
    def abox(frames):
        bb = None
        for f_ in frames:
            b = f_.split()[3].point(lambda v: 255 if v > 16 else 0).getbbox()
            if b: bb = b if bb is None else (min(bb[0], b[0]), min(bb[1], b[1]), max(bb[2], b[2]), max(bb[3], b[3]))
        return list(map(int, bb))
    E['hook']['box'] = abox(list(hfn(H['parts'], E['hook']['side'], E['hook']['dur'], E['hook']['cy']))[::4])
    lb = R.alpha_union_box(R.A('logo.motion'), LMJ['w'], LMJ['h'], 2); E['logo_motion']['box'] = [lb[0] + LMJ['x'], lb[1] + LMJ['y'], lb[2] + LMJ['x'], lb[3] + LMJ['y']]
    # ---- rule checks (SOP 2.5 = SOP v2 rhythm rules) ----
    errs, warns = list(R.safe_zone_errors(E)), []
    hend = E['hook']['start'] + E['hook']['dur']; hs = sorted(x['start'] for x in E['headings'])
    if hs and not RULES['first_heading'][0] <= hs[0] - hend <= RULES['first_heading'][1] + 1.0: warns.append(f'first heading {hs[0] - hend:.1f}s after hook (rule 5-15 s, +1 s delay)')
    evs = sorted([(E['hook']['start'], hend)] + [(x['start'], x['start'] + x['dur']) for k in ('headings', 'cards') for x in E[k]] + [(z['start'], z['end']) for z in E['zooms']] + [(t, t + E['logo_motion']['dur']) for t in E['logo_motion']['times']] + [(b, b) for b in bt])
    last = 0; quiet = []
    for a, b in evs:
        if a - last > RULES['max_no_event']: quiet.append([round(last, 1), round(a, 1)])
        last = max(last, b)
    if D - last > RULES['max_no_event']: quiet.append([round(last, 1), round(D, 1)])
    if quiet: errs.append(f'>30 s without any event: {quiet}')
    fg = sorted((x['start'], x['id'], x['dur']) for k in ('headings', 'cards') for x in E[k])
    for a, b in zip(fg, fg[1:]):
        if b[0] - a[0] < RULES['close_warn']: warns.append(f'{a[1]} and {b[1]} only {b[0] - a[0]:.1f}s apart')
        if a[0] + a[2] > b[0]: errs.append(f'{a[1]} and {b[1]} overlap')
    zmin = max(1, int(round(RULES['zoom_per_min'] * D / 60)))
    if len(E['zooms']) < zmin: warns.append(f'{len(E["zooms"])} zooms; rule is ~3/min (>= {zmin})')
    for t in E['logo_motion']['times'] + [x['start'] for x in E['headings']] + [x['start'] for x in E['cards']]:
        if t > D: errs.append(f'event at {t}s after the end of the cut ({D}s)')
    ld = E['logo_motion']['dur']
    for t in E['logo_motion']['times']:
        for h in E['headings']:
            hs, he = h['start'], h['start'] + h['dur']
            if not (t + ld <= hs + 1e-6 or t >= he - 1e-6):
                errs.append(f'logo @{t:.2f} overlaps heading {h["id"]} [{hs:.2f},{he:.2f}]')
    se = R.validate(E, os.path.join(APPD, 'edit_schema.json'))
    E['v9_cuts'] = dict(source=raw_name, source_duration=dur, speed=SPEED, fade_ms=8, keep=keep, removed=plan.get('cut_raw', []) + (pause_cuts(words) if plan.get('auto_pause_cuts', True) else []), trim_head=plan.get('trim_head'), trim_tail=plan.get('trim_tail'))
    json.dump(E, open(out_p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('cut duration', D, 's; pieces', len(keep), '; bumpers', bt); print('schema:', se or 'VALID'); print('rule errors:', errs or 'none'); print('warnings:', warns or 'none')
    return 1 if (se or errs) else 0
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--suggest':   # python v9_log.py --suggest FOR_MOSI_DIR EP  -> pause cuts + sentence-rule suggestions
        d_, ep_ = sys.argv[2], sys.argv[3]; ws = json.load(open(os.path.join(d_, ep_ + '.words.json'), encoding='utf-8-sig'))
        for c in pause_cuts(ws): print('AUTO ', c)
        for c in suggest_cuts(ws): print('CHECK', c)
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == '--skip': sys.exit(write_skip(sys.argv[2], sys.argv[3] if len(sys.argv) > 4 else None, sys.argv[-1]))
    sys.exit(main(*sys.argv[1:4]))
