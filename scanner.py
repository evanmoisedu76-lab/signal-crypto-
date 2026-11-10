"""
Scanner de signaux Hyperliquid -> notifications ntfy.

Ce script NE PASSE AUCUN ORDRE. Il lit des données publiques et t'envoie des
propositions de trades. Ce n'est pas un conseil financier.

Stratégie (bougies 1 heure, dernière bougie CLÔTURÉE uniquement) :
  LONG  : EMA20 > EMA50, clôture au-dessus du plus haut des 20 bougies
          précédentes, RSI entre 50 et 75, volume >= 1,5x la moyenne.
  SHORT : l'inverse (EMA20 < EMA50, cassure du plus bas, RSI entre 25 et 50).
  Stop  : 1,5 x ATR(14) ; Objectif : 2 x la distance du stop (ratio 1:2).
"""

import json
import os
import time
import urllib.error
import urllib.request

API = "https://api.hyperliquid.xyz/info"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
TOP_N = int(os.environ.get("TOP_N", "100"))
CAPITAL = float(os.environ.get("CAPITAL", "1000"))      # ton capital en $
RISK_PCT = float(os.environ.get("RISK_PCT", "1"))       # % du capital risqué par trade
MIN_DAY_VOLUME = float(os.environ.get("MIN_DAY_VOLUME", "1000000"))  # volume 24h minimum en $
SEND_EMPTY = os.environ.get("SEND_EMPTY", "1") == "1"   # message silencieux si aucun signal
PAUSE = float(os.environ.get("PAUSE", "1.2"))           # pause entre requêtes (limite d'API)
MAX_SIGNALS = 8


# ---------- Réseau ----------

def post_json(url, payload, retries=4):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    last_error = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError as e:
            last_error = e
            time.sleep(3)
    raise RuntimeError(f"Échec de la requête : {last_error}")


def notify(title, body, priority="default", tags="chart_with_upwards_trend"):
    if not NTFY_TOPIC:
        print(f"[ntfy non configuré] {title}\n{body}")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": tags},
    )
    urllib.request.urlopen(req, timeout=30)


# ---------- Données Hyperliquid ----------

def get_top_coins():
    """Les TOP_N cryptos les plus échangées sur Hyperliquid (volume 24h)."""
    meta, ctxs = post_json(API, {"type": "metaAndAssetCtxs"})
    rows = []
    for asset, ctx in zip(meta["universe"], ctxs):
        if asset.get("isDelisted"):
            continue
        volume = float(ctx.get("dayNtlVlm") or 0)
        funding = float(ctx.get("funding") or 0)
        if volume >= MIN_DAY_VOLUME:
            rows.append({"coin": asset["name"], "volume": volume, "funding": funding})
    rows.sort(key=lambda r: r["volume"], reverse=True)
    return rows[:TOP_N]


def get_candles(coin):
    """Bougies 1h clôturées (on retire la bougie en cours)."""
    end = int(time.time() * 1000)
    start = end - 130 * 3600 * 1000
    raw = post_json(
        API,
        {"type": "candleSnapshot",
         "req": {"coin": coin, "interval": "1h", "startTime": start, "endTime": end}},
    )
    candles = []
    for c in raw:
        if int(c["T"]) >= end:  # bougie pas encore clôturée
            continue
        candles.append({
            "o": float(c["o"]), "h": float(c["h"]), "l": float(c["l"]),
            "c": float(c["c"]), "v": float(c["v"]),
        })
    return candles


# ---------- Indicateurs ----------

def ema(values, n):
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, n=14):
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_gain, avg_loss = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(d, 0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-d, 0)) / n
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def atr(candles, n=14):
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


# ---------- Stratégie ----------

def analyze(coin, candles):
    if len(candles) < 60:
        return None
    closes = [c["c"] for c in candles]
    last = candles[-1]
    prev = candles[-21:-1]
    high20 = max(c["h"] for c in prev)
    low20 = min(c["l"] for c in prev)
    avg_vol = sum(c["v"] for c in prev) / len(prev)
    vol_ratio = last["v"] / avg_vol if avg_vol > 0 else 0

    e20, e50 = ema(closes, 20), ema(closes, 50)
    r = rsi(closes)
    a = atr(candles)
    entry = last["c"]

    side = None
    if e20 > e50 and entry > high20 and 50 <= r <= 75 and vol_ratio >= 1.5:
        side = "LONG"
    elif e20 < e50 and entry < low20 and 25 <= r <= 50 and vol_ratio >= 1.5:
        side = "SHORT"
    if side is None:
        return None

    dist = 1.5 * a
    if side == "LONG":
        stop, target = entry - dist, entry + 2 * dist
    else:
        stop, target = entry + dist, entry - 2 * dist
    stop_pct = dist / entry
    risk_usd = CAPITAL * RISK_PCT / 100
    size_usd = risk_usd / stop_pct

    return {
        "coin": coin, "side": side, "entry": entry, "stop": stop, "target": target,
        "stop_pct": stop_pct * 100, "target_pct": 2 * stop_pct * 100,
        "size_usd": size_usd, "risk_usd": risk_usd,
        "rsi": r, "vol_ratio": vol_ratio,
    }


def fmt(x):
    return f"{x:.6g}"


def format_signal(s, funding):
    return (
        f"{s['side']} {s['coin']}\n"
        f"Entrée ~{fmt(s['entry'])} | Stop {fmt(s['stop'])} (-{s['stop_pct']:.1f}%) "
        f"| Objectif {fmt(s['target'])} (+{s['target_pct']:.1f}%)\n"
        f"Taille ~{s['size_usd']:.0f}$ (risque {s['risk_usd']:.0f}$) | "
        f"RSI {s['rsi']:.0f} | volume x{s['vol_ratio']:.1f} | funding/h {funding * 100:.4f}%"
    )


# ---------- Programme principal ----------

def main():
    coins = get_top_coins()
    print(f"{len(coins)} cryptos à analyser")
    signals, errors = [], 0

    for row in coins:
        try:
            candles = get_candles(row["coin"])
            s = analyze(row["coin"], candles)
            if s:
                s["funding"] = row["funding"]
                signals.append(s)
        except Exception as e:  # une crypto en erreur ne bloque pas les autres
            errors += 1
            print(f"Erreur sur {row['coin']} : {e}")
        time.sleep(PAUSE)

    if errors == len(coins) and coins:
        notify("Scanner : erreur", "Impossible de lire les données Hyperliquid.",
               priority="high", tags="warning")
        return

    if signals:
        signals.sort(key=lambda s: s["vol_ratio"], reverse=True)
        body = "\n\n".join(format_signal(s, s["funding"]) for s in signals[:MAX_SIGNALS])
        body += "\n\nPropositions uniquement, pas un conseil financier."
        notify(f"{len(signals)} signal(s) Hyperliquid", body)
    elif SEND_EMPTY:
        notify("Scanner Hyperliquid", f"Aucun signal ({len(coins)} cryptos analysées).",
               priority="min", tags="zzz")
    print(f"Terminé : {len(signals)} signal(s), {errors} erreur(s)")


if __name__ == "__main__":
    main()
