#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ショート動画用・縦型（9:16）書き出し
====================================
まんがスタジオが生成した漫画ページ（1024×1536）をコマ単位で切り出し、
1080×1920 の縦型フレームに組み直します。1コマ＝動画の1カットです。

なぜコマ単位にするか:
  - 4コマページをスマホ全画面で出すと吹き出しが小さすぎて読めない
  - Codex のレート制限（実測 約25呼び出し/5時間）に対し、
    1ページ4コマ＝4カットなので、1コマずつ生成するより約4倍の尺が作れる

コマ座標は「コマ割りテンプレ」画像から自動検出し、読み順は
docs/rules/manga_prompt_rules.md の対応表（正本）に従って並べます。

単体でも使えます:
    python3 shorts_export.py --job-dir output/manga_studio/<ジョブID>
    python3 shorts_export.py --page page01_v1.png --template テンプレ8 --out shorts/
"""
from __future__ import annotations

import argparse
import json
import unicodedata
from collections import deque
from pathlib import Path

from PIL import Image, ImageFilter

# --- 出力仕様 --- #
SHORT_W, SHORT_H = 1080, 1920      # 9:16
SIDE_MARGIN = 48                   # コマ左右の余白
BG_BLUR = 28                       # 背景ぼかし強度
BG_DARKEN = 0.45                   # 背景を暗くする係数（0=真っ黒, 1=そのまま）

# docs/rules/manga_prompt_rules.md のテンプレ対応表を相対アンカーで表現したもの。
# 検出した矩形を、この順番で最も近いアンカーに割り当てて読み順を決める。
ANCHORS: dict[str, list[tuple[float, float]]] = {
    "テンプレ1":  [(.50, .50)],
    "テンプレ2":  [(.50, .27), (.50, .73)],
    "テンプレ3":  [(.50, .21), (.50, .66)],
    "テンプレ4":  [(.50, .34), (.50, .79)],
    "テンプレ5":  [(.50, .20), (.50, .50), (.50, .80)],
    "テンプレ6":  [(.50, .27), (.71, .73), (.28, .73)],
    "テンプレ7":  [(.71, .27), (.28, .27), (.50, .73)],
    "テンプレ8":  [(.50, .20), (.71, .50), (.28, .50), (.50, .80)],
    "テンプレ9":  [(.50, .20), (.71, .65), (.28, .50), (.29, .80)],
    "テンプレ10": [(.50, .21), (.71, .51), (.71, .80), (.28, .65)],
}

_PANEL_CACHE: dict[str, list[tuple[int, int, int, int]]] = {}


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def detect_panels(template_path: Path, dark_thresh: int = 128,
                  min_area_ratio: float = 0.01, scale: int = 4
                  ) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    """テンプレ画像から、枠線に囲まれた白い領域＝コマの矩形を検出する。"""
    im = Image.open(template_path).convert("L")
    W, H = im.size
    w, h = W // scale, H // scale
    small = im.resize((w, h), Image.BILINEAR).load()
    seen = bytearray(w * h)
    comps: list[tuple[int, int, int, int]] = []
    for sy in range(h):
        for sx in range(w):
            i = sy * w + sx
            if seen[i] or small[sx, sy] < dark_thresh:
                continue
            q = deque([(sx, sy)]); seen[i] = 1
            x0 = x1 = sx; y0 = y1 = sy; area = 0; edge = False
            while q:
                cx, cy = q.popleft(); area += 1
                if cx in (0, w - 1) or cy in (0, h - 1):
                    edge = True
                x0 = min(x0, cx); x1 = max(x1, cx)
                y0 = min(y0, cy); y1 = max(y1, cy)
                for nx, ny in ((cx+1, cy), (cx-1, cy), (cx, cy+1), (cx, cy-1)):
                    if 0 <= nx < w and 0 <= ny < h:
                        j = ny * w + nx
                        if not seen[j] and small[nx, ny] >= dark_thresh:
                            seen[j] = 1; q.append((nx, ny))
            # 画像の縁に接する領域＝ページ外の余白なので除外
            if not edge and area >= min_area_ratio * w * h:
                comps.append((x0 * scale, y0 * scale, (x1 + 1) * scale, (y1 + 1) * scale))
    return W, H, comps


def order_panels(template_name: str, W: int, H: int,
                 rects: list[tuple[int, int, int, int]]
                 ) -> list[tuple[int, int, int, int]]:
    """正本の読み順（上→下・右→左、縦長コマの例外含む）に並べ替える。"""
    anchors = ANCHORS.get(_nfc(template_name))
    if not anchors:
        # 未知のテンプレは素直に 上→下・右→左
        return sorted(rects, key=lambda r: (r[1], -r[0]))
    remaining = list(rects)
    out = []
    for ax, ay in anchors:
        if not remaining:
            break
        tx, ty = ax * W, ay * H
        best = min(remaining,
                   key=lambda r: ((r[0]+r[2])/2 - tx) ** 2 + ((r[1]+r[3])/2 - ty) ** 2)
        remaining.remove(best); out.append(best)
    out.extend(remaining)   # アンカー数を超えた分は末尾に
    return out


def panels_for_template(template_dir: Path, template_name: str
                        ) -> list[tuple[int, int, int, int]]:
    key = _nfc(template_name)
    if key in _PANEL_CACHE:
        return _PANEL_CACHE[key]
    path = None
    for p in template_dir.iterdir():
        if p.is_file() and _nfc(p.stem) == key and p.suffix.lower() in (".jpg", ".jpeg", ".png"):
            path = p; break
    if path is None:
        raise FileNotFoundError(f"テンプレが見つかりません: {template_name}")
    W, H, rects = detect_panels(path)
    ordered = order_panels(key, W, H, rects)
    _PANEL_CACHE[key] = ordered
    return ordered


def compose_vertical(panel: Image.Image) -> Image.Image:
    """1コマを 1080×1920 の縦型フレームに配置する（背景は自身のぼかし拡大）。"""
    panel = panel.convert("RGB")

    # 背景: コマ自体を画面いっぱいに拡大してぼかし、暗くする
    scale = max(SHORT_W / panel.width, SHORT_H / panel.height)
    bw, bh = int(panel.width * scale), int(panel.height * scale)
    bg = panel.resize((bw, bh), Image.LANCZOS).filter(ImageFilter.GaussianBlur(BG_BLUR))
    bg = bg.crop(((bw - SHORT_W) // 2, (bh - SHORT_H) // 2,
                  (bw - SHORT_W) // 2 + SHORT_W, (bh - SHORT_H) // 2 + SHORT_H))
    bg = Image.blend(Image.new("RGB", (SHORT_W, SHORT_H), (0, 0, 0)), bg, BG_DARKEN)

    # 前景: 余白に収まる最大サイズで中央に配置
    max_w = SHORT_W - SIDE_MARGIN * 2
    max_h = SHORT_H - SIDE_MARGIN * 2
    f = min(max_w / panel.width, max_h / panel.height)
    fw, fh = max(1, int(panel.width * f)), max(1, int(panel.height * f))
    fg = panel.resize((fw, fh), Image.LANCZOS)
    bg.paste(fg, ((SHORT_W - fw) // 2, (SHORT_H - fh) // 2))
    return bg


def slice_page(page_path: Path, template_dir: Path, template_name: str,
               inset: int = 6) -> list[Image.Image]:
    """1ページ画像をコマ単位に切り出す（読み順で返す）。

    inset: 枠線を巻き込まないよう内側に少し詰める量（px）。生成画像の枠位置は
           テンプレと完全一致するとは限らないため、わずかに内側を取る。
    """
    page = Image.open(page_path).convert("RGB")
    rects = panels_for_template(template_dir, template_name)
    out = []
    for (x0, y0, x1, y1) in rects:
        # テンプレと生成画像のサイズが違う場合に備えて比率で換算
        sx = page.width / 1024
        sy = page.height / 1536
        box = (int(x0 * sx) + inset, int(y0 * sy) + inset,
               int(x1 * sx) - inset, int(y1 * sy) - inset)
        box = (max(0, box[0]), max(0, box[1]),
               min(page.width, box[2]), min(page.height, box[3]))
        if box[2] - box[0] < 10 or box[3] - box[1] < 10:
            continue
        out.append(page.crop(box))
    return out


def export_job(job_dir: Path, template_dir: Path, out_dir: Path | None = None) -> list[Path]:
    """ジョブフォルダ内の全ページを縦型フレームに書き出す。"""
    job_json = job_dir / "job.json"
    if not job_json.exists():
        raise FileNotFoundError(f"job.json がありません: {job_dir}")
    spec = json.loads(job_json.read_text(encoding="utf-8"))
    tmpl_by_page = {int(r["ページ番号"]): r["使用するコマ割りテンプレ"].strip()
                    for r in spec.get("rows", []) if str(r.get("ページ番号", "")).strip().isdigit()}

    out_dir = out_dir or (job_dir / "shorts")
    out_dir.mkdir(parents=True, exist_ok=True)

    pages = []
    for p in job_dir.iterdir():
        if p.is_file() and p.stem.startswith("page") and p.suffix.lower() in (".png", ".jpg", ".jpeg"):
            try:
                pages.append((int(p.stem[4:6]), p))
            except ValueError:
                continue
    # 同じページの複数バージョンは最新（末尾）だけ使う
    latest: dict[int, Path] = {}
    for pn, p in sorted(pages):
        latest[pn] = p

    written: list[Path] = []
    cut = 1
    for pn in sorted(latest):
        tname = tmpl_by_page.get(pn)
        if not tname:
            continue
        for panel in slice_page(latest[pn], template_dir, tname):
            frame = compose_vertical(panel)
            dest = out_dir / f"cut{cut:03d}_p{pn:02d}.png"
            frame.save(dest, "PNG")
            written.append(dest); cut += 1
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description="漫画ページを縦型(9:16)ショート用フレームに書き出す")
    ap.add_argument("--job-dir", type=Path, help="output/manga_studio/<ジョブID>")
    ap.add_argument("--page", type=Path, help="単一ページ画像")
    ap.add_argument("--template", help="--page 使用時のテンプレ名（例: テンプレ8）")
    ap.add_argument("--template-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()

    tdir = a.template_dir
    if tdir is None:
        root = Path(__file__).resolve().parent
        cands = [d for d in root.iterdir()
                 if d.is_dir() and _nfc(d.name).startswith("コマ割りテンプレ")]
        if not cands:
            print("[!] コマ割りテンプレ フォルダが見つかりません"); return 2
        tdir = cands[0]

    if a.job_dir:
        files = export_job(a.job_dir, tdir, a.out)
    elif a.page and a.template:
        out = a.out or Path("shorts")
        out.mkdir(parents=True, exist_ok=True)
        files = []
        for i, panel in enumerate(slice_page(a.page, tdir, a.template), 1):
            dest = out / f"cut{i:03d}.png"
            compose_vertical(panel).save(dest, "PNG"); files.append(dest)
    else:
        ap.print_help(); return 2

    for f in files:
        print(f"  {f}")
    print(f"\n=== {len(files)} カットを書き出しました ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
