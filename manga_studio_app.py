#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
まんがスタジオ（コミクル代替・ローカル版）
============================================
「育成型AI漫画生産ハーネス」フォルダの直下にこのファイルを置いて実行してください。

配置場所（重要）:
    育成型AI漫画生産ハーネス/
    ├── manga_studio_app.py   ← このファイルをここに置く
    ├── scripts/
    │   └── codex_render_from_csv.py   ← 既存のこのスクリプトをそのまま再利用します
    ├── コマ割りテンプレ (1024 x 1536 px)/
    ├── キャラ参照/
    └── output/
        └── manga_studio/     ← このアプリの生成物・進捗はここに保存されます（自動作成）

前提:
    - Codex CLI がインストール・ログイン済み（`codex --version` が通ること）
    - pip install flask Pillow --break-system-packages
    - 画像生成は Codex CLI 組み込みの image_gen のみ。外部APIキーは一切使いません。

起動方法:
    cd 育成型AI漫画生産ハーネス/
    python3 manga_studio_app.py
    → ブラウザで http://127.0.0.1:5151 を開く

    別フォルダから起動したい場合は環境変数でルートを明示できます:
    MANGA_HARNESS_ROOT=/path/to/育成型AI漫画生産ハーネス python3 manga_studio_app.py

できること:
    - Manual Generation Panel: ページ番号・テンプレ選択・プロンプト入力→1枚だけ生成
    - CSV Bulk Generation: 既存フォーマットのCSV（ページ番号/使用するコマ割りテンプレ/漫画作成のプロンプト）
      をアップロードしてページ範囲を一括生成
    - Panel Layout Library: コマ割りテンプレフォルダの画像を一覧表示（10種）
    - Character Image Library: キャラ参照フォルダへの画像アップロード・削除
    - Output Format: PNG / JPEG 切り替え（PNG生成後にJPEG変換）
    - 一時停止・再開・完全停止: ページ単位で進捗をJSONに保存。完全停止は実行中の
      codex プロセスも即座に終了させる。中断したジョブは一覧から再開できる。

実装メモ:
    scripts/codex_render_from_csv.py は **無改変** で再利用します。ただし
    (a) 完全停止で実行中の codex を殺せるようにするため
    (b) macOS(NFD) と Linux(NFC) のファイル名正規化差を吸収するため
    起動時にモジュール属性を差し替えています（下部 _patch_core() を参照）。
"""
from __future__ import annotations

import csv as csv_mod
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from pathlib import Path
from uuid import uuid4

from flask import Flask, request, jsonify, send_file, Response

# --- 既存スクリプトを import して再利用（無改変・二重実装しない） --- #
ROOT = Path(os.environ.get("MANGA_HARNESS_ROOT") or Path(__file__).resolve().parent).resolve()
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
# codex_render_from_csv 側の PROJECT_ROOT 解決をこちらのルートに合わせる
os.environ.setdefault("PROJECT_ROOT", str(ROOT))

try:
    import codex_render_from_csv as core  # noqa: E402
except ImportError as e:
    print(f"[!] scripts/codex_render_from_csv.py が見つかりません: {e}", file=sys.stderr)
    print(f"    このファイルはプロジェクト直下（{ROOT}）に置いてください。", file=sys.stderr)
    print("    または MANGA_HARNESS_ROOT=... でハーネスのルートを指定してください。", file=sys.stderr)
    sys.exit(1)


# ------------------------------------------------ ファイル名正規化ユーティリティ --- #
# macOS はファイル名を NFD で保持する（「プ」= フ + ゚）。Linux は与えられたバイト列を
# そのまま保持するため、ソース中の NFC リテラル "コマ割りテンプレ..." と一致しない。
# 両対応するため、実在するエントリを NFC 比較で引き当てる。

def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _natkey(s: str):
    """テンプレ1 < テンプレ2 < テンプレ10 の順に並べるための自然順キー。"""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", _nfc(s))]


def _resolve_child(parent: Path, name: str, *, want_dir: bool = False) -> Path:
    """parent 直下から name（NFC比較）に一致する実在エントリを返す。無ければ素のパス。"""
    plain = parent / name
    if plain.exists():
        return plain
    if parent.exists():
        target = _nfc(name)
        for child in parent.iterdir():
            if want_dir and not child.is_dir():
                continue
            if _nfc(child.name) == target:
                return child
    return plain


def _resolve_template_file(name: str) -> Path:
    """"テンプレ1" → 実在する テンプレ1.jpg / .png を NFC比較で引き当てる。"""
    stem = _nfc(name).strip()
    if not TEMPLATE_DIR.exists():
        raise FileNotFoundError(f"テンプレフォルダが見つかりません: {TEMPLATE_DIR}")
    for p in TEMPLATE_DIR.iterdir():
        if p.is_file() and _nfc(p.stem) == stem and p.suffix.lower() in (".jpg", ".jpeg", ".png"):
            return p
    raise FileNotFoundError(f"テンプレが見つかりません: {TEMPLATE_DIR}/{stem}")


TEMPLATE_DIR = _resolve_child(ROOT, core.TEMPLATE_DIR.name, want_dir=True)
CHAR_REF_DIR = _resolve_child(ROOT, core.CHAR_REF_DIR.name, want_dir=True)
OUT_DIR = ROOT / "output" / "manga_studio"
OUT_DIR.mkdir(parents=True, exist_ok=True)
CHAR_REF_DIR.mkdir(parents=True, exist_ok=True)

CSV_COLUMNS = ("ページ番号", "使用するコマ割りテンプレ", "漫画作成のプロンプト")

app = Flask(__name__)


# ------------------------------------------------------ codex 実行（停止可能） --- #
_PROC_LOCK = threading.Lock()
_ACTIVE_PROC: subprocess.Popen | None = None
_STOP_EVENT = threading.Event()


def _killable_run_codex_exec(prompt_text: str, char_refs: list[Path],
                             template_path: Path, cwd: Path) -> tuple[int, str, str]:
    """core.run_codex_exec と同じ契約。違いは Popen で実行し、完全停止で kill できる点。

    停止済みなら codex を起動せず即座に失敗を返すので、core 側のリトライループは
    残り回数を空回りして即終了する（＝停止が確実に効く）。
    """
    global _ACTIVE_PROC
    if _STOP_EVENT.is_set():
        return 130, "", "stopped by user"

    cmd = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write"]
    for ref in char_refs:
        cmd.extend(["-i", str(ref)])
    cmd.extend(["-i", str(template_path), "-"])

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, cwd=str(cwd))
    with _PROC_LOCK:
        _ACTIVE_PROC = proc
    try:
        stdout, stderr = proc.communicate(prompt_text)
    finally:
        with _PROC_LOCK:
            _ACTIVE_PROC = None
    return proc.returncode, stdout, stderr


def _kill_active_codex() -> None:
    with _PROC_LOCK:
        proc = _ACTIVE_PROC
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _patch_core() -> None:
    """codex_render_from_csv を無改変のまま、実行時だけ差し替える。"""
    core.TEMPLATE_DIR = TEMPLATE_DIR
    core.CHAR_REF_DIR = CHAR_REF_DIR
    core.resolve_template_path = _resolve_template_file   # NFD/NFC 差の吸収
    core.run_codex_exec = _killable_run_codex_exec        # 完全停止の即時反映


_patch_core()


def codex_available() -> tuple[bool, str]:
    exe = shutil.which("codex")
    if not exe:
        return False, "codex コマンドが PATH にありません"
    try:
        p = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=15)
        if p.returncode == 0:
            return True, (p.stdout or p.stderr).strip().splitlines()[0]
        return False, f"codex --version が失敗しました (rc={p.returncode})"
    except Exception as e:  # noqa: BLE001
        return False, f"codex の確認に失敗しました: {e}"


# -------------------------------------------------------------- ジョブ管理 --- #
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def job_dir(job_id: str) -> Path:
    return OUT_DIR / job_id


def progress_path(job_id: str) -> Path:
    return job_dir(job_id) / "progress.json"


def spec_path(job_id: str) -> Path:
    return job_dir(job_id) / "job.json"


def load_progress(job_id: str) -> dict:
    p = progress_path(job_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"done": [], "failed": []}


def save_progress(job_id: str, data: dict) -> None:
    progress_path(job_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_spec(job_id: str, rows: list[dict], fmt: str) -> None:
    spec_path(job_id).write_text(json.dumps(
        {"rows": rows, "format": fmt, "created": time.time()},
        ensure_ascii=False, indent=2), encoding="utf-8")


def convert_format(png_path: Path, fmt: str) -> Path:
    """JPEG 指定なら変換して PNG は削除する（一覧の重複を防ぐ）。"""
    if fmt.lower() != "jpeg":
        return png_path
    from PIL import Image

    jpg_path = png_path.with_suffix(".jpg")
    with Image.open(png_path) as im:
        im.convert("RGB").save(jpg_path, "JPEG", quality=92)
    png_path.unlink(missing_ok=True)
    return jpg_path


def running_job_id() -> str | None:
    with JOBS_LOCK:
        for jid, st in JOBS.items():
            if st["status"] in ("running", "paused"):
                return jid
    return None


def validate_rows(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """CSV/手動入力の行を検証し、(有効な行, 警告メッセージ) を返す。"""
    if not rows:
        return [], ["対象ページが0件です"]
    missing = [c for c in CSV_COLUMNS if c not in rows[0]]
    if missing:
        return [], [f"CSVに必要な列がありません: {', '.join(missing)}"
                    f"（必要な列: {', '.join(CSV_COLUMNS)}）"]
    good, warns = [], []
    for i, r in enumerate(rows, start=1):
        raw = (r.get("ページ番号") or "").strip()
        if not raw.isdigit():
            warns.append(f"{i}行目: ページ番号が数値ではないためスキップしました（{raw!r}）")
            continue
        if not (r.get("使用するコマ割りテンプレ") or "").strip():
            warns.append(f"{i}行目: テンプレ名が空のためスキップしました")
            continue
        if not (r.get("漫画作成のプロンプト") or "").strip():
            warns.append(f"{i}行目: プロンプトが空のためスキップしました")
            continue
        good.append(r)
    return good, warns


def run_job(job_id: str, rows: list[dict], out_dir: Path, fmt: str,
            char_ref_dirs: list[str] | None) -> None:
    """バックグラウンドスレッドで1ページずつ生成する（一時停止・停止対応）。"""
    state = JOBS[job_id]
    try:
        progress = load_progress(job_id)
        done_pages = set(progress.get("done", []))
        failed_log = out_dir / "failed.log"

        refs = core.set_char_refs(char_ref_dirs, None)
        state["log"].append(
            f"[info] キャラ参照 {len(refs)}件: {[p.name for p in refs] or 'なし（AIが自動設計）'}")

        total = len(rows)
        for idx, row in enumerate(rows, start=1):
            if state["stop_requested"]:
                state["status"] = "stopped"
                state["log"].append("[stop] 完全停止しました")
                return

            while state["pause_requested"] and not state["stop_requested"]:
                state["status"] = "paused"
                time.sleep(0.5)
            if state["stop_requested"]:
                state["status"] = "stopped"
                state["log"].append("[stop] 完全停止しました")
                return
            state["status"] = "running"

            page_no = int(row["ページ番号"])
            state["progress"] = {"index": idx, "total": total}
            if page_no in done_pages:
                state["log"].append(f"[skip] page {page_no} は生成済み")
                continue

            state["current_page"] = page_no
            state["log"].append(f"[start] page {page_no} 生成開始（{idx}/{total}）")
            try:
                ok = core.render_one_page(row, out_dir, max_retry=3, failed_log=failed_log)
            except Exception as e:  # noqa: BLE001
                ok = False
                state["log"].append(f"[error] page {page_no}: {e}")
                traceback.print_exc()

            if state["stop_requested"]:
                state["status"] = "stopped"
                state["log"].append("[stop] 完全停止しました")
                return

            if ok:
                produced = sorted(out_dir.glob(f"page{page_no:02d}_v*.png"), key=lambda p: _natkey(p.name))
                if produced:
                    final = convert_format(produced[-1], fmt)
                    state["log"].append(f"[ok] page {page_no} → {final.name}")
                done_pages.add(page_no)
                progress["done"] = sorted(done_pages)
                save_progress(job_id, progress)
            else:
                progress.setdefault("failed", [])
                if page_no not in progress["failed"]:
                    progress["failed"].append(page_no)
                save_progress(job_id, progress)
                state["log"].append(f"[ng] page {page_no} 失敗（failed.log に記録）")

        state["current_page"] = None
        state["status"] = "completed"
        state["log"].append("[done] すべての対象ページを処理しました")
    except Exception as e:  # noqa: BLE001
        # ここで捕まえないとスレッドが黙って死に、UI が running のまま固まる
        state["status"] = "error"
        state["error"] = str(e)
        state["log"].append(f"[fatal] ジョブが異常終了しました: {e}")
        traceback.print_exc()


def start_job(rows: list[dict], fmt: str, char_ref_dirs: list[str] | None,
              job_id: str | None = None) -> str:
    job_id = job_id or uuid4().hex[:12]
    out_dir = job_dir(job_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    save_spec(job_id, rows, fmt)
    _STOP_EVENT.clear()
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "pause_requested": False,
            "stop_requested": False,
            "current_page": None,
            "progress": {"index": 0, "total": len(rows)},
            "log": [],
            "error": None,
            "format": fmt,
            "out_dir": str(out_dir),
        }
    t = threading.Thread(target=run_job, args=(job_id, rows, out_dir, fmt, char_ref_dirs),
                         daemon=True)
    t.start()
    return job_id


# ---------------------------------------------------------------- API --- #

def _safe_under(base: Path, rel: str) -> Path:
    """base 配下に収まる実在パスを NFC 比較で解決する（ディレクトリ脱出を防ぐ）。"""
    target = base
    for part in Path(rel).parts:
        if part in ("..", "/", ""):
            raise ValueError("不正なパスです")
        target = _resolve_child(target, part)
    resolved = target.resolve()
    if base.resolve() not in resolved.parents and resolved != base.resolve():
        raise ValueError("不正なパスです")
    if not resolved.is_file():
        raise FileNotFoundError(rel)
    return resolved


@app.route("/api/env")
def api_env():
    ok, msg = codex_available()
    return jsonify({
        "root": str(ROOT),
        "template_dir": str(TEMPLATE_DIR),
        "template_dir_exists": TEMPLATE_DIR.exists(),
        "char_ref_dir": str(CHAR_REF_DIR),
        "out_dir": str(OUT_DIR),
        "codex_ok": ok,
        "codex_msg": msg,
    })


@app.route("/api/templates")
def api_templates():
    if not TEMPLATE_DIR.exists():
        return jsonify([])
    names = [_nfc(p.stem) for p in TEMPLATE_DIR.iterdir()
             if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    return jsonify(sorted(set(names), key=_natkey))


@app.route("/api/template-image/<path:name>")
def api_template_image(name):
    try:
        return send_file(_resolve_template_file(name))
    except (FileNotFoundError, ValueError):
        return "not found", 404


@app.route("/api/characters")
def api_characters():
    """core.resolve_char_refs と同じ収集ルール（直下＋1階層・チビ版は除外）で一覧する。"""
    if not CHAR_REF_DIR.exists():
        return jsonify([])
    refs = core.resolve_char_refs([str(CHAR_REF_DIR)], None)
    out = []
    for p in refs:
        try:
            rel = p.relative_to(CHAR_REF_DIR)
        except ValueError:
            continue
        out.append(_nfc(str(rel)))
    return jsonify(sorted(out, key=_natkey))


@app.route("/api/character-image/<path:name>")
def api_character_image(name):
    try:
        return send_file(_safe_under(CHAR_REF_DIR, name))
    except (FileNotFoundError, ValueError):
        return "not found", 404


@app.route("/api/upload-character", methods=["POST"])
def api_upload_character():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "ファイルがありません"}), 400
    # secure_filename は日本語を落とすので使わず、パス区切りだけを除去する
    name = Path(f.filename.replace("\\", "/")).name
    if not name or name.startswith("."):
        return jsonify({"error": "ファイル名が不正です"}), 400
    if Path(name).suffix.lower() not in (".png", ".jpg", ".jpeg"):
        return jsonify({"error": "PNG / JPG のみアップロードできます"}), 400
    f.save(CHAR_REF_DIR / name)
    return jsonify({"ok": True, "filename": name})


@app.route("/api/delete-character", methods=["POST"])
def api_delete_character():
    data = request.get_json(force=True, silent=True) or {}
    try:
        target = _safe_under(CHAR_REF_DIR, data.get("name", ""))
    except (FileNotFoundError, ValueError):
        return jsonify({"error": "見つかりません"}), 404
    target.unlink()
    return jsonify({"ok": True})


@app.route("/api/generate/manual", methods=["POST"])
def api_generate_manual():
    if (jid := running_job_id()):
        return jsonify({"error": f"別のジョブが実行中です（{jid}）。停止してから開始してください"}), 409
    data = request.get_json(force=True, silent=True) or {}
    page_raw = str(data.get("page_no", "1")).strip()
    template = str(data.get("template", "")).strip()
    prompt = str(data.get("prompt", "")).strip()
    fmt = data.get("format", "PNG")
    if not page_raw.isdigit():
        return jsonify({"error": "ページ番号は数値で入力してください"}), 400
    if not template:
        return jsonify({"error": "コマ割りテンプレを選択してください"}), 400
    if not prompt:
        return jsonify({"error": "プロンプトを入力してください"}), 400
    try:
        _resolve_template_file(template)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 400

    row = {
        "ページ番号": page_raw,
        "使用するコマ割りテンプレ": template,
        "漫画作成のプロンプト": prompt,
    }
    return jsonify({"job_id": start_job([row], fmt, None), "warnings": []})


@app.route("/api/generate/csv", methods=["POST"])
def api_generate_csv():
    if (jid := running_job_id()):
        return jsonify({"error": f"別のジョブが実行中です（{jid}）。停止してから開始してください"}), 409
    f = request.files.get("csv")
    pages_spec = (request.form.get("pages") or "").strip()
    fmt = request.form.get("format", "PNG")
    if not f or not f.filename:
        return jsonify({"error": "CSVファイルがありません"}), 400

    try:
        text = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify({"error": "CSVはUTF-8で保存してください"}), 400
    all_rows = list(csv_mod.DictReader(io.StringIO(text)))

    rows, warns = validate_rows(all_rows)
    if not rows:
        return jsonify({"error": " / ".join(warns) or "対象ページが0件です"}), 400

    if pages_spec:
        try:
            wanted = set(core.expand_pages(pages_spec))
        except ValueError:
            return jsonify({"error": "ページ範囲の書式が不正です（例: 1 / 2-5 / 1,3,5）"}), 400
        rows = [r for r in rows if int(r["ページ番号"]) in wanted]
        if not rows:
            return jsonify({"error": f"指定ページ {pages_spec} がCSVに見つかりません"}), 400

    return jsonify({"job_id": start_job(rows, fmt, None), "warnings": warns})


@app.route("/api/jobs")
def api_jobs():
    """再開できる中断ジョブを新しい順に返す。"""
    out = []
    for d in sorted(OUT_DIR.glob("*/"), key=lambda p: p.stat().st_mtime, reverse=True):
        sp = d / "job.json"
        if not sp.exists():
            continue
        try:
            spec = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        prog = load_progress(d.name)
        total = len(spec.get("rows", []))
        done = len(prog.get("done", []))
        live = JOBS.get(d.name)
        out.append({
            "job_id": d.name,
            "total": total,
            "done": done,
            "remaining": total - done,
            "format": spec.get("format", "PNG"),
            "status": live["status"] if live else ("completed" if done >= total > 0 else "interrupted"),
        })
    return jsonify(out[:20])


@app.route("/api/job/<job_id>/restart", methods=["POST"])
def api_job_restart(job_id):
    """中断したジョブを未完了ページから再開する（アプリ再起動後もOK）。"""
    if (jid := running_job_id()):
        return jsonify({"error": f"別のジョブが実行中です（{jid}）"}), 409
    sp = spec_path(job_id)
    if not sp.exists():
        return jsonify({"error": "ジョブが見つかりません"}), 404
    spec = json.loads(sp.read_text(encoding="utf-8"))
    rows = spec.get("rows", [])
    done = set(load_progress(job_id).get("done", []))
    remaining = [r for r in rows if int(r["ページ番号"]) not in done]
    if not remaining:
        return jsonify({"error": "未完了のページはありません"}), 400
    start_job(rows, spec.get("format", "PNG"), None, job_id=job_id)
    return jsonify({"job_id": job_id, "remaining": len(remaining)})


@app.route("/api/job/<job_id>/status")
def api_job_status(job_id):
    state = JOBS.get(job_id)
    if not state:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "status": state["status"],
        "current_page": state["current_page"],
        "progress": state.get("progress"),
        "error": state.get("error"),
        "log": state["log"][-80:],
    })


def _set_flags(job_id: str, *, pause: bool | None = None, stop: bool | None = None):
    state = JOBS.get(job_id)
    if not state:
        return None
    if pause is not None:
        state["pause_requested"] = pause
    if stop is not None:
        state["stop_requested"] = stop
    return state


@app.route("/api/job/<job_id>/pause", methods=["POST"])
def api_job_pause(job_id):
    return (jsonify({"ok": True}) if _set_flags(job_id, pause=True)
            else (jsonify({"error": "not found"}), 404))


@app.route("/api/job/<job_id>/resume", methods=["POST"])
def api_job_resume(job_id):
    return (jsonify({"ok": True}) if _set_flags(job_id, pause=False)
            else (jsonify({"error": "not found"}), 404))


@app.route("/api/job/<job_id>/stop", methods=["POST"])
def api_job_stop(job_id):
    state = _set_flags(job_id, pause=False, stop=True)
    if not state:
        return jsonify({"error": "not found"}), 404
    _STOP_EVENT.set()        # これ以降 codex を新規起動させない
    _kill_active_codex()     # 実行中の codex も落とす
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>/results")
def api_job_results(job_id):
    d = job_dir(job_id)
    if not d.exists():
        return jsonify([])
    files = [p for p in d.iterdir()
             if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg")]
    files.sort(key=lambda p: _natkey(p.name))
    return jsonify([{"name": p.name, "url": f"/api/job/{job_id}/file/{p.name}"} for p in files])


@app.route("/api/job/<job_id>/file/<path:name>")
def api_job_file(job_id, name):
    try:
        return send_file(_safe_under(job_dir(job_id), name))
    except (FileNotFoundError, ValueError):
        return "not found", 404


# ---------------------------------------------------------------- UI --- #

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>まんがスタジオ（ローカル版）</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%96%8B%3C/text%3E%3C/svg%3E">
<style>
  :root {
    --bg:#0e1220; --panel:#171c2e; --panel2:#1d2338; --border:#2a3150;
    --text:#e7eaf5; --text-dim:#8b93b0; --accent:#4fb8d6; --accent2:#3a8fa8;
    --ok:#4fd67a; --ng:#e05a5a; --warn:#e0c85a;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,'Hiragino Sans','Noto Sans JP',sans-serif; }
  header { padding:18px 32px; display:flex; align-items:center; gap:16px;
    border-bottom:1px solid var(--border); flex-wrap:wrap; }
  header h1 { font-size:21px; margin:0; }
  .layout { display:grid; grid-template-columns:430px 1fr; min-height:calc(100vh - 66px); }
  .left { padding:24px; border-right:1px solid var(--border); overflow-y:auto; }
  .right { padding:24px; }
  .section { margin-bottom:26px; }
  .section h2 { font-size:14px; color:var(--accent); margin:0 0 12px; }
  label { display:block; font-size:13px; color:var(--text-dim); margin:14px 0 6px; }
  input[type=text], input[type=number], input[type=file], textarea, select {
    width:100%; background:var(--panel2); border:1px solid var(--border); border-radius:8px;
    color:var(--text); padding:10px 12px; font-size:14px; font-family:inherit; }
  textarea { min-height:170px; resize:vertical; line-height:1.6; }
  .tabs { display:flex; gap:8px; margin-bottom:10px; }
  .tab { flex:1; padding:10px; text-align:center; border-radius:8px; background:var(--panel2);
    border:1px solid var(--border); cursor:pointer; font-size:14px; user-select:none; }
  .tab.active { background:var(--accent2); border-color:var(--accent); color:#08131a; font-weight:600; }
  .btn { display:block; width:100%; padding:13px; border:none; border-radius:8px;
    font-size:15px; font-weight:600; cursor:pointer; margin-top:10px; font-family:inherit; }
  .btn:disabled { opacity:.45; cursor:not-allowed; }
  .btn-primary { background:linear-gradient(90deg,var(--accent2),var(--accent)); color:#08131a; }
  .btn-row { display:flex; gap:8px; }
  .btn-row .btn { flex:1; font-size:13px; padding:11px 6px; }
  .btn-secondary { background:var(--panel2); color:var(--text); border:1px solid var(--border); }
  .btn-danger { background:#3a1f22; color:var(--ng); border:1px solid #5a2a2e; }
  .dropzone { border:2px dashed var(--border); border-radius:10px; padding:22px; text-align:center;
    color:var(--text-dim); cursor:pointer; font-size:13px; }
  .dropzone:hover { border-color:var(--accent); color:var(--text); }
  .thumbs { display:grid; grid-template-columns:repeat(5,1fr); gap:8px; margin-top:10px; }
  .thumb { position:relative; }
  .thumb img { width:100%; aspect-ratio:2/3; object-fit:cover; border-radius:6px;
    border:1px solid var(--border); cursor:pointer; display:block; }
  .thumb img.selected { border-color:var(--accent); box-shadow:0 0 0 2px var(--accent); }
  .thumb .cap { font-size:10px; color:var(--text-dim); text-align:center; margin-top:3px;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .thumb .del { position:absolute; top:2px; right:2px; background:rgba(0,0,0,.7); color:var(--ng);
    border:none; border-radius:4px; cursor:pointer; font-size:11px; line-height:1; padding:3px 5px; }
  .badge { display:inline-block; padding:4px 11px; border-radius:20px; font-size:12px; }
  .status-running { background:#243a24; color:var(--ok); }
  .status-paused { background:#3a3624; color:var(--warn); }
  .status-stopped,.status-error { background:#3a2424; color:var(--ng); }
  .status-completed { background:#243a24; color:var(--ok); }
  #log { background:#0a0e18; border-radius:8px; padding:12px; font-family:ui-monospace,monospace;
    font-size:12px; height:210px; overflow-y:auto; margin-top:12px; white-space:pre-wrap;
    color:var(--text-dim); line-height:1.5; }
  .results { display:grid; grid-template-columns:repeat(auto-fill,minmax(190px,1fr)); gap:14px; }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:8px; }
  .card img { width:100%; border-radius:6px; display:block; }
  .card .nm { font-size:11px; color:var(--text-dim); margin-top:6px; text-align:center; }
  .placeholder { color:var(--text-dim); text-align:center; padding:90px 20px; font-size:14px; line-height:1.8; }
  .notice { padding:10px 14px; border-radius:8px; font-size:13px; margin-bottom:14px; line-height:1.6; }
  .notice-err { background:#3a2424; color:#f0b4b4; border:1px solid #5a2a2e; }
  .notice-ok { background:#1c2e24; color:#a8e0bd; border:1px solid #2a5a3e; }
  .hint { font-size:11px; color:var(--text-dim); margin-top:6px; line-height:1.6; }
  .jobrow { display:flex; align-items:center; gap:8px; font-size:12px; padding:7px 0;
    border-bottom:1px solid var(--border); }
  .jobrow button { background:var(--panel2); color:var(--accent); border:1px solid var(--border);
    border-radius:6px; padding:5px 9px; cursor:pointer; font-size:11px; }
</style>
</head>
<body>
<header>
  <h1>🖋 まんがスタジオ</h1>
  <span style="font-size:12px;color:var(--text-dim);">ローカル版・Codex CLI (image_gen) 駆動 / 外部APIキー不使用</span>
  <span id="env-badge" class="badge" style="margin-left:auto;"></span>
</header>
<div class="layout">
  <div class="left">
    <div id="env-warn"></div>

    <div class="section">
      <h2>① Character Image Library（キャラ参照）</h2>
      <div class="dropzone" id="char-drop">クリックして画像をアップロード<br>
        <span style="font-size:11px;">キャラ参照/ に保存。空ならAIが自動設計します</span></div>
      <input type="file" id="char-file" accept="image/png,image/jpeg" style="display:none">
      <div class="thumbs" id="char-thumbs"></div>
    </div>

    <div class="section">
      <h2>② Panel Layout Library（コマ割りテンプレ）</h2>
      <div class="thumbs" id="tmpl-thumbs"></div>
      <div class="hint">クリックで Manual Generation のテンプレに反映されます</div>
    </div>

    <div class="section">
      <h2>③ Generation Mode</h2>
      <div class="tabs">
        <div class="tab active" data-mode="manual">Manual Generation</div>
        <div class="tab" data-mode="csv">CSV Bulk Generation</div>
      </div>
      <label>Output Format</label>
      <div class="tabs">
        <div class="tab active" data-fmt="PNG">PNG</div>
        <div class="tab" data-fmt="JPEG">JPEG</div>
      </div>
      <div class="hint" id="fmt-hint">PNG: 高品質（1024×1536 そのまま保存）</div>
    </div>

    <div class="section" id="manual-panel">
      <h2>④ Manual Generation Panel</h2>
      <label>ページ番号</label>
      <input type="number" id="m-page" value="1" min="1">
      <label>使用するコマ割りテンプレ</label>
      <select id="m-template"><option value="">Select a template...</option></select>
      <label>漫画作成のプロンプト</label>
      <textarea id="m-prompt" placeholder="漫画の全体設定: …&#10;作画のスタイル: …&#10;登場キャラクター: …&#10;ページのストーリー:&#10;1コマ目: …"></textarea>
      <button class="btn btn-primary" id="btn-manual">⚡ Start Generation</button>
    </div>

    <div class="section" id="csv-panel" style="display:none;">
      <h2>④ CSV Bulk Generation</h2>
      <label>CSVファイル（3列: ページ番号 / 使用するコマ割りテンプレ / 漫画作成のプロンプト）</label>
      <input type="file" id="csv-file" accept=".csv">
      <label>ページ範囲（空欄 = CSV全行）</label>
      <input type="text" id="csv-pages" placeholder="例: 1 / 2-5 / 1,3,5">
      <button class="btn btn-primary" id="btn-csv">⚡ Start Generation</button>
    </div>

    <div class="section" id="job-controls" style="display:none;">
      <h2>ジョブ制御</h2>
      <span class="badge" id="status-badge">-</span>
      <span id="progress-text" style="font-size:12px;color:var(--text-dim);margin-left:8px;"></span>
      <div class="btn-row" style="margin-top:10px;">
        <button class="btn btn-secondary" id="btn-pause">⏸ 一時停止</button>
        <button class="btn btn-secondary" id="btn-resume">▶ 再開</button>
        <button class="btn btn-danger" id="btn-stop">⏹ 完全停止</button>
      </div>
      <div id="log"></div>
    </div>

    <div class="section" id="jobs-section" style="display:none;">
      <h2>中断したジョブ（未完了ページから再開）</h2>
      <div id="jobs-list"></div>
    </div>
  </div>

  <div class="right">
    <div id="results-area">
      <div class="placeholder">Generation results will appear here.<br>
        設定を選んで「Start Generation」を押してください。</div>
    </div>
  </div>
</div>

<script>
let mode = 'manual', fmt = 'PNG', currentJob = null, pollTimer = null;
const $ = (id) => document.getElementById(id);

document.querySelectorAll('.tab[data-mode]').forEach(t => t.onclick = () => {
  mode = t.dataset.mode;
  document.querySelectorAll('.tab[data-mode]').forEach(x => x.classList.toggle('active', x === t));
  $('manual-panel').style.display = mode === 'manual' ? 'block' : 'none';
  $('csv-panel').style.display = mode === 'csv' ? 'block' : 'none';
});
document.querySelectorAll('.tab[data-fmt]').forEach(t => t.onclick = () => {
  fmt = t.dataset.fmt;
  document.querySelectorAll('.tab[data-fmt]').forEach(x => x.classList.toggle('active', x === t));
  $('fmt-hint').textContent = fmt === 'PNG'
    ? 'PNG: 高品質（1024×1536 そのまま保存）'
    : 'JPEG: 軽量（PNG生成後に品質92で変換。PNGは残しません）';
});

async function loadEnv() {
  const e = await (await fetch('/api/env')).json();
  const badge = $('env-badge');
  badge.textContent = e.codex_ok ? 'Codex CLI OK' : 'Codex CLI 未検出';
  badge.className = 'badge ' + (e.codex_ok ? 'status-completed' : 'status-error');
  let html = '';
  if (!e.codex_ok) html += `<div class="notice notice-err"><b>画像生成できません:</b> ${e.codex_msg}<br>
    ターミナルで <code>codex login</code> を実行し、<code>codex --version</code> が通る状態にしてください。</div>`;
  if (!e.template_dir_exists) html += `<div class="notice notice-err">
    <b>コマ割りテンプレのフォルダが見つかりません:</b><br>${e.template_dir}</div>`;
  $('env-warn').innerHTML = html;
}

async function loadTemplates() {
  const names = await (await fetch('/api/templates')).json();
  const sel = $('m-template'), thumbs = $('tmpl-thumbs');
  sel.innerHTML = '<option value="">Select a template...</option>';
  thumbs.innerHTML = '';
  names.forEach(n => {
    sel.add(new Option(n, n));
    const d = document.createElement('div'); d.className = 'thumb';
    const img = document.createElement('img');
    img.src = '/api/template-image/' + encodeURIComponent(n); img.title = n; img.loading = 'lazy';
    img.onclick = () => { sel.value = n; syncTemplateSelection(); };
    d.appendChild(img);
    const cap = document.createElement('div'); cap.className = 'cap'; cap.textContent = n;
    d.appendChild(cap); thumbs.appendChild(d);
  });
  sel.onchange = syncTemplateSelection;
}
function syncTemplateSelection() {
  const v = $('m-template').value;
  document.querySelectorAll('#tmpl-thumbs img').forEach(i => i.classList.toggle('selected', i.title === v));
}

async function loadCharacters() {
  const names = await (await fetch('/api/characters')).json();
  const thumbs = $('char-thumbs');
  thumbs.innerHTML = '';
  names.forEach(n => {
    const d = document.createElement('div'); d.className = 'thumb';
    const img = document.createElement('img');
    img.src = '/api/character-image/' + n.split('/').map(encodeURIComponent).join('/');
    img.title = n; img.loading = 'lazy';
    const del = document.createElement('button');
    del.className = 'del'; del.textContent = '✕'; del.title = '削除';
    del.onclick = async () => {
      if (!confirm(n + ' を削除しますか？')) return;
      await fetch('/api/delete-character', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({name: n})});
      loadCharacters();
    };
    d.append(img, del);
    const cap = document.createElement('div'); cap.className = 'cap';
    cap.textContent = n; d.appendChild(cap);
    thumbs.appendChild(d);
  });
}

$('char-drop').onclick = () => $('char-file').click();
$('char-file').onchange = async (e) => {
  const f = e.target.files[0]; if (!f) return;
  const fd = new FormData(); fd.append('file', f);
  const r = await (await fetch('/api/upload-character', {method:'POST', body: fd})).json();
  if (r.error) alert(r.error);
  e.target.value = ''; loadCharacters();
};

async function loadJobs() {
  const jobs = await (await fetch('/api/jobs')).json();
  const open = jobs.filter(j => j.remaining > 0 && ['interrupted','stopped','error'].includes(j.status));
  $('jobs-section').style.display = open.length ? 'block' : 'none';
  $('jobs-list').innerHTML = '';
  open.forEach(j => {
    const row = document.createElement('div'); row.className = 'jobrow';
    row.innerHTML = `<span style="flex:1;">${j.job_id} — 残り ${j.remaining}/${j.total}ページ</span>`;
    const b = document.createElement('button'); b.textContent = '▶ 再開';
    b.onclick = async () => {
      const r = await (await fetch(`/api/job/${j.job_id}/restart`, {method:'POST'})).json();
      if (r.error) { alert(r.error); return; }
      attachJob(j.job_id);
    };
    row.appendChild(b); $('jobs-list').appendChild(row);
  });
}

$('btn-manual').onclick = async () => {
  const body = { page_no: $('m-page').value, template: $('m-template').value,
                 prompt: $('m-prompt').value, format: fmt };
  const r = await (await fetch('/api/generate/manual', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)})).json();
  if (r.error) { alert(r.error); return; }
  attachJob(r.job_id);
};

$('btn-csv').onclick = async () => {
  const f = $('csv-file').files[0];
  if (!f) { alert('CSVファイルを選択してください'); return; }
  const fd = new FormData();
  fd.append('csv', f); fd.append('pages', $('csv-pages').value); fd.append('format', fmt);
  const r = await (await fetch('/api/generate/csv', {method:'POST', body: fd})).json();
  if (r.error) { alert(r.error); return; }
  if (r.warnings && r.warnings.length) alert('注意:\n' + r.warnings.join('\n'));
  attachJob(r.job_id);
};

function attachJob(jobId) {
  currentJob = jobId;
  $('job-controls').style.display = 'block';
  $('results-area').innerHTML = '<div class="results" id="results"></div>';
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(poll, 1500);
  poll();
}

async function poll() {
  if (!currentJob) return;
  const res = await fetch(`/api/job/${currentJob}/status`);
  if (!res.ok) return;
  const data = await res.json();
  const badge = $('status-badge');
  badge.textContent = data.status;
  badge.className = 'badge status-' + data.status;
  const p = data.progress;
  $('progress-text').textContent = (p && p.total) ? `${p.index}/${p.total} ページ` : '';
  const log = $('log');
  log.textContent = (data.log || []).join('\n');
  log.scrollTop = log.scrollHeight;

  const files = await (await fetch(`/api/job/${currentJob}/results`)).json();
  const results = $('results');
  if (results) {
    results.innerHTML = '';
    files.forEach(f => {
      const c = document.createElement('div'); c.className = 'card';
      const a = document.createElement('a'); a.href = f.url; a.target = '_blank';
      const img = document.createElement('img'); img.src = f.url; img.loading = 'lazy';
      a.appendChild(img);
      const nm = document.createElement('div'); nm.className = 'nm'; nm.textContent = f.name;
      c.append(a, nm); results.appendChild(c);
    });
    if (!files.length) results.innerHTML =
      '<div class="placeholder">生成中です…<br>1ページあたり数分かかることがあります。</div>';
  }
  if (['completed', 'stopped', 'error'].includes(data.status)) {
    clearInterval(pollTimer); pollTimer = null; loadJobs();
    if (data.status === 'error' && data.error) alert('ジョブが異常終了しました: ' + data.error);
  }
}

const ctl = (path) => () => { if (currentJob) fetch(`/api/job/${currentJob}/${path}`, {method:'POST'}); };
$('btn-pause').onclick = ctl('pause');
$('btn-resume').onclick = ctl('resume');
$('btn-stop').onclick = () => {
  if (currentJob && confirm('完全停止しますか？（実行中のcodexも終了します）')) ctl('stop')();
};

loadEnv(); loadTemplates(); loadCharacters(); loadJobs();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


if __name__ == "__main__":
    ok, msg = codex_available()
    print("=" * 68)
    print(f"プロジェクトルート : {ROOT}")
    print(f"テンプレフォルダ   : {TEMPLATE_DIR} (存在: {TEMPLATE_DIR.exists()})")
    print(f"キャラ参照フォルダ : {CHAR_REF_DIR} (存在: {CHAR_REF_DIR.exists()})")
    print(f"出力フォルダ       : {OUT_DIR}")
    print(f"Codex CLI          : {'OK - ' + msg if ok else '[!] ' + msg}")
    if not ok:
        print("  → `codex login` を済ませてから生成してください（画像生成は image_gen のみ使用）")
    print("=" * 68)
    print("起動: http://127.0.0.1:5151")
    app.run(host="127.0.0.1", port=5151, debug=False, threaded=True)
