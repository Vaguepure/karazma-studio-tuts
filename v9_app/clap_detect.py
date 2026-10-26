# Clap detector + clap-based cut marking (teacher protocol: stutter -> clap once -> ~2 s silence -> restart WHOLE sentence).
# usage:
#   python clap_detect.py PROJECT [--wav work/audio48k.wav] [--words work/fa_words.json] [--merge work/manual_edits.json]
#   -> work/claps.json  {claps:[{t, peak_db, crest_db, flat, hf, sil_s}], cuts:[[s, e, "clap", reason], ...]}
#   --merge appends the cuts to cut_raw of the given manual_edits file (backup *.bak_clap first). Never run on a live job's file
#   without a snapshot. Library use: detect_claps(x, sr) / clap_cuts(claps, words).
import sys, os, json, subprocess
import numpy as np

P = dict(hop=0.0025, win=0.010, rise_db=18.0, lead=0.060, abs_db=-26.0, over_speech_db=0.0, crest_db=11.0,
         burst_max=0.120, decay_db=20.0, cover_min=0.6, hf_min=0.35, sil_from=0.15, sil_to=1.20, sil_db=-42.0,
         sil_frac=0.85, min_sil=1.2, min_gap=0.8)

def load(path, sr=48000):
    # -threads 2: clap decode must not peg every core (v9.1)
    raw = subprocess.run(["ffmpeg", "-threads", "2", "-v", "error", "-i", path, "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32)/32768, sr

def _env(x, sr, hop, win):
    h, w = int(hop*sr), int(win*sr); n = max(0, (len(x)-w)//h)
    idx = np.arange(n)[:, None]*h + np.arange(w)[None, :]
    fr = x[idx]; rms = np.sqrt((fr**2).mean(1))+1e-9
    return 20*np.log10(rms), 20*np.log10(np.abs(fr).max(1)+1e-9)

def detect_claps(x, sr, p=P, debug=None):
    hop = p["hop"]; db, pk = _env(x, sr, hop, p["win"])
    # speech level reference: median of active frames (10 ms window); claps must clearly exceed it
    act = db[db > -45]; speech = np.median(act) if len(act) else -30
    lead = int(p["lead"]/hop); out = []
    i = lead
    while i < len(db)-2:
        prev = np.median(db[i-lead:i])
        if db[i]-prev < p["rise_db"] or db[i] < max(p["abs_db"], speech+p["over_speech_db"]) - 6: i += 1; continue
        j = i + int(np.argmax(db[i:i+int(0.02/hop)])); peak = db[j]
        if peak < max(p["abs_db"], speech+p["over_speech_db"]): i += 1; continue
        # burst must decay by decay_db within burst_max
        k = j; lim = j+int(p["burst_max"]/hop)
        while k < min(lim, len(db)-1) and db[k] > peak-p["decay_db"]: k += 1
        if db[k] > peak-p["decay_db"]: i = k; continue
        # crest factor (peak/rms) over 30 ms around the peak, spectral shape over 20 ms
        c = int(j*hop*sr); seg = x[max(0, c-int(0.005*sr)):c+int(0.025*sr)]
        crest = 20*np.log10(np.abs(seg).max()/(np.sqrt((seg**2).mean())+1e-9))
        s2 = x[max(0, c-int(0.003*sr)):c+int(0.017*sr)]*np.hanning(len(x[max(0, c-int(0.003*sr)):c+int(0.017*sr)]))
        S = np.abs(np.fft.rfft(s2))**2+1e-12; f = np.fft.rfftfreq(len(s2), 1/sr); band = (f > 300) & (f < min(12000, sr/2-1))
        flat = float(np.exp(np.mean(np.log(S[band])))/np.mean(S[band])); hf = float(S[f > 2000].sum()/S[f > 100].sum())
        edges = 500*2**(np.arange(0, 4.01, 0.5)); bands = np.array([S[(f >= lo) & (f < hi)].sum() for lo, hi in zip(edges[:-1], edges[1:])])
        cover = float(np.mean(10*np.log10(bands/bands.max()+1e-12) > -20))  # share of half-octave bands 0.5-8 kHz within 20 dB of the loudest
        # ~silence after the burst
        a, b = j+int(p["sil_from"]/hop), j+int(p["sil_to"]/hop); tail = db[a:b]
        silfrac = float(np.mean(tail < p["sil_db"])) if len(tail) else 0.0
        if debug is not None: debug.append(dict(t=round(j*hop,3), peak=round(float(peak),1), crest=round(float(crest),1), flat=round(flat,2), cover=round(cover,2), hf=round(hf,2), sil=round(silfrac,2)))
        ok = crest >= p["crest_db"] and cover >= p["cover_min"] and hf >= p["hf_min"] and silfrac >= p["sil_frac"]
        if ok:
            t = j*hop+p["win"]/2; e = a
            while e < len(db) and db[e] < p["sil_db"]: e += 1
            if (e-j)*hop >= p["min_sil"] and (not out or t-out[-1]["t"] > p["min_gap"]):
                out.append(dict(t=round(t, 3), peak_db=round(float(peak), 1), crest_db=round(float(crest), 1), flat=round(flat, 2), cover=round(cover, 2),
                                hf=round(hf, 2), sil_s=round((e-j)*hop, 2), speech_db=round(float(speech), 1)))
            i = j+int(p["min_gap"]/hop)
        else:
            i = k+1
    return out

END = tuple(".؟?!…")
def clap_cuts(claps, words, sent_gap=0.35, pad=0.03, max_back=None):
    """For each clap: cut [start of broken sentence, start of next word after clap). Broken sentence = words since the
    previous sentence boundary (punctuation or a gap >= sent_gap) before the clap."""
    W = sorted(words, key=lambda w: w["s"]); cuts = []
    for c in claps:
        t = c["t"]; before = [i for i, w in enumerate(W) if w["e"] <= t+0.02]
        after = [w for w in W if w["s"] >= t+0.05]
        end = (after[0]["s"]-pad) if after else t+c.get("sil_s", 1.0)
        if not before: cuts.append([round(max(0, t-0.05), 3), round(end, 3), "clap", "clap with no preceding words"]); continue
        k = before[-1]; st = k
        while st > 0 and not W[st-1]["w"].rstrip().endswith(END) and W[st]["s"]-W[st-1]["e"] < sent_gap and (max_back is None or t-W[st-1]["s"] <= max_back): st -= 1
        txt = " ".join(w["w"] for w in W[st:k+1])
        nxt = " ".join(w["w"] for w in after[:6])
        cuts.append([round(W[st]["s"]-pad, 3), round(end, 3), "clap",
                     f"clap at {t:.2f}s: broken «{txt}» removed through clap+silence; keep «{nxt}…»"])
    return cuts

def _words(path):
    W = json.load(open(path)); return [dict(w=w.get("w", ""), s=float(w["s"]), e=float(w["e"])) for w in W]

if __name__ == "__main__":
    a = sys.argv[1:]; proj = a[0]
    def opt(n, d=None): return a[a.index(n)+1] if n in a else d
    wav = opt("--wav", os.path.join(proj, "work/audio48k.wav")); wj = opt("--words", os.path.join(proj, "work/fa_words.json"))
    x, sr = load(wav); C = detect_claps(x, sr); W = _words(wj) if wj and os.path.exists(wj) else []
    cuts = clap_cuts(C, W) if W else []
    out = opt("--out", os.path.join(proj, "work/claps.json"))
    json.dump(dict(wav=wav, params=P, claps=C, cuts=cuts), open(out, "w"), ensure_ascii=False, indent=1)
    print(f"{len(C)} clap(s) in {wav} ({len(x)/sr:.1f}s) -> {out}")
    for c in cuts: print("  cut", c)
    m = opt("--merge")
    if m and cuts:
        import shutil; shutil.copy(m, m+".bak_clap"); E = json.load(open(m)); E.setdefault("cut_raw", [])
        have = {(round(c[0], 2), round(c[1], 2)) for c in E["cut_raw"]}
        E["cut_raw"] += [c for c in cuts if (round(c[0], 2), round(c[1], 2)) not in have]
        E["cut_raw"].sort(key=lambda c: c[0]); json.dump(E, open(m, "w"), ensure_ascii=False, indent=1); print("merged into", m)
