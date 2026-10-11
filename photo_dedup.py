#!/usr/bin/env python3
"""
사진 정리 도우미 (Photo Dedup)

사진 폴더 전체를 분석해서
  1) 내용이 완전히 같은(바이트 단위로 동일한) 중복 파일을 찾고,
     사용자가 확인한 뒤 직접 삭제할 수 있게 해 주고,
  2) 모든 사진을 촬영 날짜 / 크기 / 이름 / 경로 순으로 둘러볼 수 있게 해 줍니다.

내 컴퓨터에서만 동작하는 웹 화면(http://127.0.0.1:8765)을 띄웁니다.
파일은 외부로 전송되지 않습니다.

사용법:
    python photo_dedup.py                     # 화면에서 폴더 입력
    python photo_dedup.py "D:\\사진"           # 바로 스캔 시작
    python photo_dedup.py "D:\\사진" "E:\\백업"  # 여러 폴더를 함께 비교

표준 라이브러리만으로 동작합니다. 아래를 설치하면 더 좋아집니다(선택):
    pip install Pillow pillow-heif send2trash
      - Pillow       : 모든 사진의 빠른 썸네일 생성
      - pillow-heif  : 아이폰 HEIC 사진 썸네일
      - send2trash   : 삭제 시 휴지통으로 보내기
"""

import argparse
import csv
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from PIL import Image, ImageOps

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

HAS_HEIF = False
if HAS_PIL:
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        HAS_HEIF = True
    except ImportError:
        pass

try:
    import send2trash

    HAS_TRASH = True
except ImportError:
    HAS_TRASH = False

APP_DIR = Path(__file__).resolve().parent / ".photo_dedup"
THUMB_DIR = APP_DIR / "thumbs"
LOG_FILE = APP_DIR / "delete_log.csv"
QUARANTINE_SUFFIX = "_중복삭제대기"

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp",
    ".heic", ".heif", ".avif",
}
RAW_EXTS = {
    ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng", ".orf",
    ".rw2", ".raf", ".pef", ".srw", ".x3f", ".3fr", ".kdc", ".mrw",
}
VIDEO_EXTS = {
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".mts", ".m2ts", ".3gp",
    ".mpg", ".mpeg", ".webm",
}
MEDIA_EXTS = IMAGE_EXTS | RAW_EXTS | VIDEO_EXTS
BROWSER_IMG = {".jpg", ".jpeg", ".jpe", ".png", ".gif", ".webp", ".bmp", ".avif"}
BROWSER_VIDEO = {".mp4", ".m4v", ".webm", ".mov"}
# EXIF(TIFF 구조)를 직접 읽을 수 있는 형식
EXIF_EXTS = {".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".dng", ".nef", ".cr2",
             ".arw", ".orf", ".pef", ".srw", ".nrw", ".sr2", ".srf"}
SKIP_DIRS = {"$recycle.bin", "system volume information", ".photo_dedup",
             ".trash", ".trashes", "@eadir", ".thumbnails"}

QUICK = 64 * 1024        # 빠른 비교용: 앞/뒤 64KB
CHUNK = 1024 * 1024      # 전체 해시 읽기 단위
EXIF_HEAD = 256 * 1024   # EXIF 읽기용 앞부분


# ---------------------------------------------------------------------------
# EXIF / 날짜 추출 (표준 라이브러리만 사용)
# ---------------------------------------------------------------------------

EXIF_DATE_RE = re.compile(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
FNAME_DATE_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
    r"(?:[-_ T]?([01]\d|2[0-3])[-_.]?([0-5]\d)[-_.]?([0-5]\d))?(?!\d)"
)


def _valid_date(y, mo, d, h=0, mi=0, s=0):
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
    except ValueError:
        return None
    if not (1980 <= dt.year <= datetime.now().year + 1):
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_tiff(buf, base, res):
    bo = buf[base:base + 2]
    if bo == b"II":
        e = "<"
    elif bo == b"MM":
        e = ">"
    else:
        return

    def u16(o):
        return struct.unpack_from(e + "H", buf, base + o)[0]

    def u32(o):
        return struct.unpack_from(e + "I", buf, base + o)[0]

    def read_ifd(off):
        n = u16(off)
        if n > 500:
            raise ValueError("bad ifd")
        entries = {}
        for k in range(n):
            p = off + 2 + 12 * k
            entries[u16(p)] = (u16(p + 2), u32(p + 4), p + 8)
        return entries, u32(off + 2 + 12 * n)

    def val(entry):
        typ, cnt, p = entry
        if typ == 2:
            o = p if cnt <= 4 else u32(p)
            return buf[base + o:base + o + cnt].split(b"\0")[0].decode("ascii", "ignore")
        if typ == 3:
            return u16(p)
        if typ == 4:
            return u32(p)
        return None

    try:
        ifd0, nxt = read_ifd(u32(4))
        date = None
        if 0x8769 in ifd0:
            exif, _ = read_ifd(val(ifd0[0x8769]))
            for tag in (0x9003, 0x9004):  # DateTimeOriginal, DateTimeDigitized
                if tag in exif:
                    date = val(exif[tag])
                    if date and EXIF_DATE_RE.match(date):
                        break
        if (not date or not EXIF_DATE_RE.match(date)) and 0x0132 in ifd0:
            date = val(ifd0[0x0132])
        if date:
            m = EXIF_DATE_RE.match(date)
            if m:
                res["date"] = _valid_date(*m.groups())
        if 0x0112 in ifd0:
            res["orientation"] = val(ifd0[0x0112])
        if nxt:
            ifd1, _ = read_ifd(nxt)
            if 0x0201 in ifd1 and 0x0202 in ifd1:
                off, ln = val(ifd1[0x0201]), val(ifd1[0x0202])
                if off and ln:
                    res["thumb_off"] = base + off
                    res["thumb_len"] = ln
    except (struct.error, ValueError, IndexError, TypeError):
        pass


def parse_exif(head):
    """JPEG/TIFF 앞부분에서 촬영일, 회전값, 내장 썸네일 위치를 읽는다."""
    res = {}
    if head[:2] == b"\xff\xd8":
        i = 2
        while i + 4 <= len(head):
            if head[i] != 0xFF:
                break
            marker = head[i + 1]
            if marker == 0xFF:
                i += 1
                continue
            if marker in (0xDA, 0xD9):
                break
            seglen = struct.unpack(">H", head[i + 2:i + 4])[0]
            if marker == 0xE1 and head[i + 4:i + 10] == b"Exif\x00\x00":
                _parse_tiff(head, i + 10, res)
                break
            i += 2 + seglen
    elif head[:4] in (b"II*\x00", b"MM\x00*"):
        _parse_tiff(head, 0, res)
    return res


def date_from_name(name):
    m = FNAME_DATE_RE.search(name)
    if not m:
        return None
    y, mo, d, h, mi, s = m.groups()
    return _valid_date(y, mo, d, h or 0, mi or 0, s or 0)


# ---------------------------------------------------------------------------
# 데이터베이스
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    dir TEXT, name TEXT, ext TEXT,
    size INTEGER, mtime REAL,
    taken TEXT, date_src TEXT,
    orientation INTEGER, thumb_off INTEGER, thumb_len INTEGER,
    quick_hash TEXT, full_hash TEXT,
    meta_done INTEGER DEFAULT 0,
    seen INTEGER
);
CREATE INDEX IF NOT EXISTS ix_size ON files(size);
CREATE INDEX IF NOT EXISTS ix_full ON files(full_hash);
CREATE INDEX IF NOT EXISTS ix_taken ON files(taken);
"""


def db_path_for(roots):
    key = "|".join(sorted(os.path.normcase(r) for r in roots))
    return APP_DIR / ("index_%s.sqlite" % hashlib.sha1(key.encode("utf-8")).hexdigest()[:12])


def connect(path):
    con = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


DUP_GROUPS_SQL = """
    SELECT full_hash, size, COUNT(*) AS cnt FROM files
    WHERE full_hash IS NOT NULL AND full_hash NOT LIKE 'ERR%'
    GROUP BY full_hash, size HAVING cnt > 1
"""


# ---------------------------------------------------------------------------
# 스캔
# ---------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.roots = []
        self.db = None
        self.all_files = False
        self.scanning = False
        self.cancel = False
        self.phase = "idle"
        self.done = 0
        self.total = 0
        self.bytes_done = 0
        self.bytes_total = 0
        self.current = ""
        self.errors = 0
        self.message = ""
        self.started = 0.0
        self.finished = 0.0

    def progress(self):
        with self.lock:
            return {
                "roots": self.roots, "scanning": self.scanning, "phase": self.phase,
                "done": self.done, "total": self.total,
                "bytes_done": self.bytes_done, "bytes_total": self.bytes_total,
                "current": self.current, "errors": self.errors,
                "message": self.message,
                "elapsed": (self.finished or time.time()) - self.started if self.started else 0,
            }


STATE = State()


def set_phase(phase, total=0, bytes_total=0):
    with STATE.lock:
        STATE.phase = phase
        STATE.done = 0
        STATE.total = total
        STATE.bytes_done = 0
        STATE.bytes_total = bytes_total
        STATE.current = ""


def iter_files(root, all_files):
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            with STATE.lock:
                STATE.errors += 1
            continue
        with it:
            for ent in it:
                if STATE.cancel:
                    return
                try:
                    if ent.is_dir(follow_symlinks=False):
                        low = ent.name.lower()
                        if low in SKIP_DIRS or ent.name.endswith(QUARANTINE_SUFFIX):
                            continue
                        stack.append(ent.path)
                    elif ent.is_file(follow_symlinks=False):
                        ext = os.path.splitext(ent.name)[1].lower()
                        if not all_files and ext not in MEDIA_EXTS:
                            continue
                        st = ent.stat(follow_symlinks=False)
                        yield ent.path, d, ent.name, ext, st.st_size, st.st_mtime
                except OSError:
                    with STATE.lock:
                        STATE.errors += 1


def quick_hash(path, size):
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        if size <= 2 * QUICK:
            h.update(f.read())
        else:
            h.update(f.read(QUICK))
            f.seek(size - QUICK)
            h.update(f.read(QUICK))
    return h.hexdigest()


def full_hash(path):
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as f:
        while True:
            if STATE.cancel:
                raise InterruptedError
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            with STATE.lock:
                STATE.bytes_done += len(b)
    return h.hexdigest()


def run_scan(roots, all_files):
    con = connect(STATE.db)
    try:
        con.executescript(SCHEMA)
        scan_id = int(time.time())

        # 1단계: 폴더 탐색
        set_phase("walk")
        batch = []
        upsert = """
            INSERT INTO files(path, dir, name, ext, size, mtime, seen)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
                quick_hash = CASE WHEN files.size = excluded.size AND files.mtime = excluded.mtime
                                  THEN files.quick_hash END,
                full_hash  = CASE WHEN files.size = excluded.size AND files.mtime = excluded.mtime
                                  THEN files.full_hash END,
                meta_done  = CASE WHEN files.size = excluded.size AND files.mtime = excluded.mtime
                                  THEN files.meta_done ELSE 0 END,
                size = excluded.size, mtime = excluded.mtime, seen = excluded.seen
        """
        for root in roots:
            for path, d, name, ext, size, mtime in iter_files(root, all_files):
                batch.append((path, d, name, ext, size, mtime, scan_id))
                with STATE.lock:
                    STATE.done += 1
                    STATE.bytes_done += size
                    STATE.current = d
                if len(batch) >= 2000:
                    con.executemany(upsert, batch)
                    con.commit()
                    batch.clear()
            if STATE.cancel:
                break
        if batch:
            con.executemany(upsert, batch)
        if STATE.cancel:
            con.commit()
            return
        con.execute("DELETE FROM files WHERE seen IS NOT ?", (scan_id,))
        con.commit()

        # 2단계: 촬영 날짜 읽기
        rows = con.execute("SELECT id, path, name, ext, mtime FROM files WHERE meta_done = 0").fetchall()
        set_phase("meta", total=len(rows))
        updates = []
        for r in rows:
            if STATE.cancel:
                break
            info = {}
            if r["ext"] in EXIF_EXTS:
                try:
                    with open(r["path"], "rb") as f:
                        info = parse_exif(f.read(EXIF_HEAD))
                except OSError:
                    with STATE.lock:
                        STATE.errors += 1
            date, src = info.get("date"), "exif"
            if not date:
                date, src = date_from_name(r["name"]), "filename"
            if not date:
                date = datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
                src = "mtime"
            updates.append((date, src, info.get("orientation"), info.get("thumb_off"),
                            info.get("thumb_len"), r["id"]))
            with STATE.lock:
                STATE.done += 1
                STATE.current = r["path"]
            if len(updates) >= 500:
                con.executemany("UPDATE files SET taken=?, date_src=?, orientation=?, thumb_off=?, "
                                "thumb_len=?, meta_done=1 WHERE id=?", updates)
                con.commit()
                updates.clear()
        if updates:
            con.executemany("UPDATE files SET taken=?, date_src=?, orientation=?, thumb_off=?, "
                            "thumb_len=?, meta_done=1 WHERE id=?", updates)
            con.commit()
        if STATE.cancel:
            return

        # 3단계: 크기가 같은 파일끼리 앞/뒤 일부만 빠르게 비교
        rows = con.execute("""
            SELECT id, path, size FROM files
            WHERE quick_hash IS NULL AND size > 0
              AND size IN (SELECT size FROM files WHERE size > 0 GROUP BY size HAVING COUNT(*) > 1)
        """).fetchall()
        set_phase("quick", total=len(rows))
        updates = []
        for r in rows:
            if STATE.cancel:
                break
            try:
                qh = quick_hash(r["path"], r["size"])
            except OSError:
                qh = "ERR%d" % r["id"]
                with STATE.lock:
                    STATE.errors += 1
            updates.append((qh, r["id"]))
            with STATE.lock:
                STATE.done += 1
                STATE.current = r["path"]
            if len(updates) >= 500:
                con.executemany("UPDATE files SET quick_hash=? WHERE id=?", updates)
                con.commit()
                updates.clear()
        if updates:
            con.executemany("UPDATE files SET quick_hash=? WHERE id=?", updates)
            con.commit()
        if STATE.cancel:
            return

        # 4단계: 후보만 전체 내용 비교
        rows = con.execute("""
            SELECT id, path, size, quick_hash FROM files
            WHERE full_hash IS NULL AND quick_hash IS NOT NULL AND quick_hash NOT LIKE 'ERR%'
              AND (size, quick_hash) IN (
                  SELECT size, quick_hash FROM files WHERE quick_hash IS NOT NULL
                  GROUP BY size, quick_hash HAVING COUNT(*) > 1)
            ORDER BY dir, name
        """).fetchall()
        big = sum(r["size"] for r in rows if r["size"] > 2 * QUICK)
        set_phase("full", total=len(rows), bytes_total=big)
        updates = []
        for r in rows:
            if STATE.cancel:
                break
            with STATE.lock:
                STATE.current = r["path"]
            if r["size"] <= 2 * QUICK:
                fh = "Q" + r["quick_hash"]  # 작은 파일은 빠른 해시가 곧 전체 해시
            else:
                try:
                    fh = full_hash(r["path"])
                except InterruptedError:
                    break
                except OSError:
                    fh = "ERR%d" % r["id"]
                    with STATE.lock:
                        STATE.errors += 1
            updates.append((fh, r["id"]))
            with STATE.lock:
                STATE.done += 1
            if len(updates) >= 100:
                con.executemany("UPDATE files SET full_hash=? WHERE id=?", updates)
                con.commit()
                updates.clear()
        if updates:
            con.executemany("UPDATE files SET full_hash=? WHERE id=?", updates)
            con.commit()
    except Exception as e:  # 스캔 스레드가 조용히 죽지 않도록
        with STATE.lock:
            STATE.message = "오류: %s" % e
        raise
    finally:
        con.close()
        with STATE.lock:
            STATE.scanning = False
            STATE.finished = time.time()
            if STATE.cancel:
                STATE.phase = "cancelled"
            elif not STATE.message:
                STATE.phase = "done"
            else:
                STATE.phase = "error"


def start_scan(roots, all_files=False):
    clean = []
    for r in roots:
        r = r.strip().strip('"')
        if not r:
            continue
        p = os.path.abspath(os.path.expanduser(r))
        if not os.path.isdir(p):
            raise ValueError("폴더를 찾을 수 없습니다: %s" % r)
        clean.append(p)
    if not clean:
        raise ValueError("스캔할 폴더를 입력하세요.")
    with STATE.lock:
        if STATE.scanning:
            raise ValueError("이미 스캔 중입니다.")
        STATE.roots = clean
        STATE.all_files = all_files
        STATE.db = db_path_for(clean)
        STATE.scanning = True
        STATE.cancel = False
        STATE.errors = 0
        STATE.message = ""
        STATE.started = time.time()
        STATE.finished = 0.0
    APP_DIR.mkdir(parents=True, exist_ok=True)
    remember(clean)
    threading.Thread(target=run_scan, args=(clean, all_files), daemon=True).start()


def remember(roots):
    try:
        (APP_DIR / "last.json").write_text(json.dumps(roots, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def load_last():
    try:
        return json.loads((APP_DIR / "last.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def open_existing(roots):
    """스캔하지 않고 이전 결과만 연다."""
    clean = [os.path.abspath(r.strip().strip('"')) for r in roots if r.strip()]
    db = db_path_for(clean)
    if not db.exists():
        return False
    with STATE.lock:
        STATE.roots = clean
        STATE.db = db
        STATE.phase = "done"
    remember(clean)
    return True


# ---------------------------------------------------------------------------
# 조회
# ---------------------------------------------------------------------------

def db():
    if not STATE.db or not Path(STATE.db).exists():
        return None
    return connect(STATE.db)


def thumb_kind(r):
    ext = r["ext"]
    if HAS_PIL and (ext in IMAGE_EXTS or ext in BROWSER_IMG) and (ext not in (".heic", ".heif") or HAS_HEIF):
        return "pil"
    if r["thumb_off"]:
        return "exif"
    if ext in BROWSER_IMG:
        return "orig"
    return "none"


def file_dict(r):
    ext = r["ext"]
    kind = "video" if ext in VIDEO_EXTS else ("raw" if ext in RAW_EXTS else "image")
    return {
        "id": r["id"], "path": r["path"], "dir": r["dir"], "name": r["name"], "ext": ext,
        "size": r["size"], "mtime": r["mtime"], "taken": r["taken"], "date_src": r["date_src"],
        "orientation": r["orientation"] or 1, "thumb": thumb_kind(r), "kind": kind,
        "viewable": ext in BROWSER_IMG or ext in BROWSER_VIDEO,
        "dup": r["dup"] if "dup" in r.keys() else None,
    }


def api_summary():
    con = db()
    if not con:
        return {"ready": False}
    try:
        t = con.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files").fetchone()
        d = con.execute("SELECT COUNT(*), COALESCE(SUM((cnt-1)*size),0), COALESCE(SUM(cnt),0) FROM (%s)"
                        % DUP_GROUPS_SQL).fetchone()
    except sqlite3.OperationalError:  # 스캔 직후 아직 테이블이 없을 때
        return {"ready": False}
    finally:
        con.close()
    return {"ready": True, "files": t[0], "bytes": t[1], "groups": d[0], "wasted": d[1],
            "dup_files": d[2]}


def api_duplicates(q):
    con = db()
    if not con:
        return {"groups": [], "total": 0}
    sort = q.get("sort", "wasted")
    order = {
        "wasted": "(cnt-1)*size DESC",
        "count": "cnt DESC, size DESC",
        "size": "size DESC",
        "taken": "taken DESC",
    }.get(sort, "(cnt-1)*size DESC")
    offset, limit = int(q.get("offset", 0)), min(int(q.get("limit", 30)), 200)
    folder = q.get("folder", "").strip()
    with con:
        base = DUP_GROUPS_SQL
        params = []
        if folder:
            # 지정한 폴더에 하나라도 들어있는 그룹만
            base = """SELECT g.* FROM (%s) g WHERE EXISTS (
                        SELECT 1 FROM files f WHERE f.full_hash = g.full_hash AND f.path LIKE ? ESCAPE '\\')""" % DUP_GROUPS_SQL
            params.append(like_prefix(folder))
        total = con.execute("SELECT COUNT(*) FROM (%s)" % base, params).fetchone()[0]
        groups = con.execute("""
            SELECT g.full_hash, g.size, g.cnt,
                   (SELECT MIN(taken) FROM files f WHERE f.full_hash = g.full_hash) AS taken
            FROM (%s) g ORDER BY %s LIMIT ? OFFSET ?""" % (base, order),
                             params + [limit, offset]).fetchall()
        hashes = [g["full_hash"] for g in groups]
        files = {}
        if hashes:
            marks = ",".join("?" * len(hashes))
            for r in con.execute("SELECT * FROM files WHERE full_hash IN (%s) ORDER BY taken, path"
                                 % marks, hashes):
                files.setdefault(r["full_hash"], []).append(file_dict(r))
    con.close()
    return {
        "total": total,
        "groups": [{"hash": g["full_hash"], "size": g["size"], "count": g["cnt"],
                    "wasted": (g["cnt"] - 1) * g["size"], "files": files.get(g["full_hash"], [])}
                   for g in groups],
    }


def like_prefix(s):
    s = s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return s + "%"


SIZE_BUCKETS = [
    ("lt1", "1MB 미만", 0, 1 << 20),
    ("1to5", "1 ~ 5MB", 1 << 20, 5 << 20),
    ("5to20", "5 ~ 20MB", 5 << 20, 20 << 20),
    ("20to100", "20 ~ 100MB", 20 << 20, 100 << 20),
    ("100to1g", "100MB ~ 1GB", 100 << 20, 1 << 30),
    ("gt1g", "1GB 이상", 1 << 30, 1 << 62),
]


def api_files(q):
    con = db()
    if not con:
        return {"files": [], "total": 0}
    where, params = [], []
    if q.get("year"):
        where.append("substr(taken,1,4) = ?")
        params.append(q["year"])
    if q.get("month"):
        where.append("substr(taken,6,2) = ?")
        params.append(q["month"].zfill(2))
    if q.get("ext"):
        where.append("ext = ?")
        params.append(q["ext"])
    if q.get("kind") == "image":
        where.append("ext IN (%s)" % ",".join("'%s'" % e for e in IMAGE_EXTS))
    elif q.get("kind") == "raw":
        where.append("ext IN (%s)" % ",".join("'%s'" % e for e in RAW_EXTS))
    elif q.get("kind") == "video":
        where.append("ext IN (%s)" % ",".join("'%s'" % e for e in VIDEO_EXTS))
    if q.get("bucket"):
        for key, _, lo, hi in SIZE_BUCKETS:
            if key == q["bucket"]:
                where.append("size >= ? AND size < ?")
                params += [lo, hi]
    if q.get("q"):
        where.append("path LIKE ? ESCAPE '\\'")
        params.append("%" + like_prefix(q["q"])[:-1] + "%")
    if q.get("date_src"):
        where.append("date_src = ?")
        params.append(q["date_src"])
    join = """LEFT JOIN (SELECT full_hash h, COUNT(*) dup FROM files
                         WHERE full_hash IS NOT NULL AND full_hash NOT LIKE 'ERR%'
                         GROUP BY full_hash HAVING dup > 1) d ON d.h = files.full_hash"""
    if q.get("dup") == "1":
        where.append("d.dup IS NOT NULL")
    w = ("WHERE " + " AND ".join(where)) if where else ""
    order = {
        "taken_desc": "taken DESC, path", "taken_asc": "taken ASC, path",
        "size_desc": "size DESC", "size_asc": "size ASC",
        "name": "name COLLATE NOCASE", "path": "path COLLATE NOCASE",
    }.get(q.get("sort", "taken_desc"), "taken DESC, path")
    offset, limit = int(q.get("offset", 0)), min(int(q.get("limit", 200)), 1000)
    with con:
        tot = con.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files %s %s" % (join, w),
                          params).fetchone()
        rows = con.execute("SELECT files.*, d.dup FROM files %s %s ORDER BY %s LIMIT ? OFFSET ?"
                           % (join, w, order), params + [limit, offset]).fetchall()
    con.close()
    return {"total": tot[0], "bytes": tot[1], "files": [file_dict(r) for r in rows]}


def api_stats():
    con = db()
    if not con:
        return {}
    with con:
        months = con.execute("""SELECT substr(taken,1,7) ym, COUNT(*) c, SUM(size) b FROM files
                                WHERE taken IS NOT NULL GROUP BY ym ORDER BY ym""").fetchall()
        exts = con.execute("SELECT ext, COUNT(*) c, SUM(size) b FROM files GROUP BY ext ORDER BY b DESC").fetchall()
        srcs = con.execute("SELECT date_src, COUNT(*) c FROM files GROUP BY date_src").fetchall()
        buckets = []
        for key, label, lo, hi in SIZE_BUCKETS:
            r = con.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE size >= ? AND size < ?",
                            (lo, hi)).fetchone()
            buckets.append({"key": key, "label": label, "count": r[0], "bytes": r[1]})
        folders = con.execute("""
            SELECT f.dir, COUNT(*) c, SUM(f.size) b FROM files f
            JOIN (%s) g ON g.full_hash = f.full_hash
            GROUP BY f.dir ORDER BY c DESC LIMIT 30""" % DUP_GROUPS_SQL).fetchall()
    con.close()
    return {
        "months": [{"ym": r["ym"], "count": r["c"], "bytes": r["b"]} for r in months],
        "exts": [{"ext": r["ext"] or "(없음)", "count": r["c"], "bytes": r["b"]} for r in exts],
        "date_src": {(r["date_src"] or "pending"): r["c"] for r in srcs},
        "buckets": buckets,
        "dup_folders": [{"dir": r["dir"], "count": r["c"], "bytes": r["b"]} for r in folders],
    }


def api_auto_plan(body):
    """중복 그룹마다 남길 파일 1개를 규칙으로 고르고, 나머지 id 목록을 돌려준다."""
    rule = body.get("rule", "oldest")
    prefer = os.path.normcase(body.get("prefer", "").strip())
    avoid = os.path.normcase(body.get("avoid", "").strip())
    con = db()
    if not con:
        return {"ids": [], "bytes": 0}
    with con:
        rows = con.execute("""
            SELECT f.id, f.path, f.size, f.taken, f.mtime, f.full_hash FROM files f
            JOIN (%s) g ON g.full_hash = f.full_hash ORDER BY f.full_hash""" % DUP_GROUPS_SQL).fetchall()
    con.close()
    groups = {}
    for r in rows:
        groups.setdefault(r["full_hash"], []).append(r)

    def key(r):
        if rule == "shortest":
            return (len(r["path"]), r["path"])
        if rule == "longest":
            return (-len(r["path"]), r["path"])
        if rule == "newest":
            return (-(r["mtime"] or 0), len(r["path"]))
        return (r["mtime"] or 0, len(r["path"]), r["path"])  # oldest: 가장 먼저 생긴 파일

    ids, sizes, total = [], [], 0
    for files in groups.values():
        cands = files
        if prefer:
            p = [f for f in files if os.path.normcase(f["path"]).startswith(prefer)]
            cands = p or cands
        if avoid:
            a = [f for f in cands if not os.path.normcase(f["path"]).startswith(avoid)]
            cands = a or cands
        keep = min(cands, key=key)
        for f in files:
            if f["id"] != keep["id"]:
                ids.append(f["id"])
                sizes.append(f["size"])
                total += f["size"]
    return {"ids": ids, "sizes": sizes, "bytes": total, "groups": len(groups)}


def root_of(path):
    np = os.path.normcase(path)
    for r in STATE.roots:
        nr = os.path.normcase(r).rstrip("\\/")
        if np == nr or np.startswith(nr + os.sep) or np.startswith(nr + "/"):
            return r
    return None


def quarantine_dest(path):
    root = root_of(path) or os.path.dirname(path)
    rp = Path(root)
    qroot = (rp.parent if rp.name else rp) / ((rp.name or "drive") + QUARANTINE_SUFFIX)
    dest = qroot / os.path.relpath(path, root)
    if dest.exists():
        stem, suf = dest.stem, dest.suffix
        n = 1
        while dest.exists():
            dest = dest.with_name("%s (%d)%s" % (stem, n, suf))
            n += 1
    return dest


def api_delete(body):
    ids = [int(i) for i in body.get("ids", [])]
    mode = body.get("mode", "trash" if HAS_TRASH else "move")
    if mode == "trash" and not HAS_TRASH:
        return {"error": "휴지통 기능을 쓰려면 'pip install send2trash' 가 필요합니다."}
    if mode not in ("trash", "move", "permanent"):
        return {"error": "알 수 없는 삭제 방식"}
    with STATE.lock:
        if STATE.scanning:
            return {"error": "스캔이 끝난 뒤에 삭제할 수 있습니다."}
    con = db()
    if not con or not ids:
        return {"deleted": 0, "skipped": []}
    want = set(ids)
    marks = ",".join("?" * len(ids))
    rows = con.execute("SELECT * FROM files WHERE id IN (%s)" % marks, ids).fetchall()
    hashes = {r["full_hash"] for r in rows if r["full_hash"] and not r["full_hash"].startswith("ERR")}
    members = {}
    if hashes:
        hm = ",".join("?" * len(hashes))
        for r in con.execute("SELECT id, path, size, full_hash FROM files WHERE full_hash IN (%s)" % hm,
                             list(hashes)):
            members.setdefault(r["full_hash"], []).append(r)

    deleted, freed, skipped = [], 0, []
    APP_DIR.mkdir(parents=True, exist_ok=True)
    new_log = not LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="", encoding="utf-8-sig") as lf:
        log = csv.writer(lf)
        if new_log:
            log.writerow(["시각", "방식", "원래 경로", "이동된 경로", "크기", "남긴 파일"])
        for r in rows:
            h = r["full_hash"]
            group = members.get(h, [])
            keepers = [m for m in group if m["id"] not in want]
            # 안전장치 1: 중복 그룹이 아닌 파일은 지우지 않는다.
            if len(group) < 2:
                skipped.append({"id": r["id"], "reason": "중복이 아닌 파일"})
                continue
            # 안전장치 2: 그룹의 모든 사본을 지우지 않는다(최소 1개 남김).
            alive = [k for k in keepers if os.path.isfile(k["path"]) and os.path.getsize(k["path"]) == k["size"]]
            if not alive:
                skipped.append({"id": r["id"], "reason": "남는 사본이 없어 건너뜀"})
                continue
            p = r["path"]
            try:
                if not os.path.isfile(p) or os.path.getsize(p) != r["size"]:
                    skipped.append({"id": r["id"], "reason": "파일이 바뀌었거나 없음"})
                    continue
                dest = ""
                if mode == "trash":
                    send2trash.send2trash(p)
                elif mode == "move":
                    d = quarantine_dest(p)
                    d.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(p, str(d))
                    dest = str(d)
                else:
                    os.remove(p)
            except Exception as e:
                skipped.append({"id": r["id"], "reason": str(e)})
                continue
            log.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), mode, p, dest, r["size"],
                          alive[0]["path"]])
            deleted.append(r["id"])
            freed += r["size"]
            want.discard(r["id"])
            # 같은 요청 안에서 다음 판단에 반영
            members[h] = [m for m in group if m["id"] != r["id"]]
    if deleted:
        with con:
            con.executemany("DELETE FROM files WHERE id = ?", [(i,) for i in deleted])
    con.close()
    return {"deleted": len(deleted), "freed": freed, "skipped": skipped, "deleted_ids": deleted}


def api_reveal(body):
    con = db()
    if not con:
        return {}
    r = con.execute("SELECT path FROM files WHERE id = ?", (int(body.get("id", 0)),)).fetchone()
    con.close()
    if not r:
        return {"error": "없는 파일"}
    p = r["path"]
    if sys.platform.startswith("win"):
        subprocess.Popen('explorer /select,"%s"' % p)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", p])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(p)])
    return {"ok": True}


def make_thumb(r):
    kind = thumb_kind(r)
    if kind == "pil":
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        cache = THUMB_DIR / ("%d_%d.jpg" % (r["id"], int(r["mtime"] or 0)))
        if cache.exists():
            return cache.read_bytes(), "image/jpeg"
        try:
            with Image.open(r["path"]) as im:
                im.draft("RGB", (480, 480))
                im = ImageOps.exif_transpose(im)
                im.thumbnail((360, 360))
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                im.save(cache, "JPEG", quality=80)
            return cache.read_bytes(), "image/jpeg"
        except Exception:
            pass  # 아래 방식으로 대체
    if r["thumb_off"] and r["thumb_len"]:
        try:
            with open(r["path"], "rb") as f:
                f.seek(r["thumb_off"])
                data = f.read(r["thumb_len"])
            if data[:2] == b"\xff\xd8":
                return data, "image/jpeg"
        except OSError:
            pass
    if r["ext"] in BROWSER_IMG:
        try:
            return Path(r["path"]).read_bytes(), mimetypes.guess_type(r["name"])[0] or "image/jpeg"
        except OSError:
            pass
    return None, None


# ---------------------------------------------------------------------------
# 웹 서버
# ---------------------------------------------------------------------------

HTML_PATH = Path(__file__).resolve().parent / "photo_dedup.html"


class Handler(BaseHTTPRequestHandler):
    server_version = "PhotoDedup/1.0"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8",
                                  {"Cache-Control": "no-store"})
            if u.path == "/api/status":
                p = STATE.progress()
                p["summary"] = api_summary()
                p["features"] = {"pil": HAS_PIL, "heif": HAS_HEIF, "trash": HAS_TRASH,
                                 "platform": sys.platform, "suffix": QUARANTINE_SUFFIX}
                return self._send(200, p)
            if u.path == "/api/duplicates":
                return self._send(200, api_duplicates(q))
            if u.path == "/api/files":
                return self._send(200, api_files(q))
            if u.path == "/api/stats":
                return self._send(200, api_stats())
            if u.path.startswith("/thumb/") or u.path.startswith("/file/"):
                return self._serve_file(u.path)
            return self._send(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._send(500, {"error": str(e)})
            except Exception:
                pass

    def _serve_file(self, path):
        kind, _, sid = path.strip("/").partition("/")
        con = db()
        if not con:
            return self._send(404, {"error": "no db"})
        r = con.execute("SELECT * FROM files WHERE id = ?", (int(sid),)).fetchone()
        con.close()
        if not r:
            return self._send(404, {"error": "no file"})
        cache = {"Cache-Control": "private, max-age=86400"}
        if kind == "thumb":
            data, ctype = make_thumb(r)
            if data is None:
                return self._send(404, {"error": "no thumb"})
            return self._send(200, data, ctype, cache)
        # 원본 파일 스트리밍 (동영상 탐색을 위해 Range 지원)
        p = r["path"]
        try:
            size = os.path.getsize(p)
        except OSError:
            return self._send(404, {"error": "missing"})
        ctype = mimetypes.guess_type(r["name"])[0] or "application/octet-stream"
        if r["ext"] == ".mov":
            ctype = "video/mp4"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                b = f.read(min(CHUNK, left))
                if not b:
                    break
                self.wfile.write(b)
                left -= len(b)

    def do_POST(self):
        # 다른 웹사이트가 이 서버에 요청을 보내지 못하도록 Host와 사용자 정의 헤더를 확인
        if not self._host_ok() or self.headers.get("X-Photo-Dedup") != "1":
            return self._send(403, {"error": "forbidden"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/api/scan":
                start_scan(body.get("roots", []), bool(body.get("all_files")))
                return self._send(200, {"ok": True})
            if u.path == "/api/open":
                ok = open_existing(body.get("roots", []))
                return self._send(200, {"ok": ok})
            if u.path == "/api/cancel":
                STATE.cancel = True
                return self._send(200, {"ok": True})
            if u.path == "/api/auto_plan":
                return self._send(200, api_auto_plan(body))
            if u.path == "/api/delete":
                return self._send(200, api_delete(body))
            if u.path == "/api/reveal":
                return self._send(200, api_reveal(body))
            return self._send(404, {"error": "not found"})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})


def main():
    ap = argparse.ArgumentParser(description="사진 폴더 중복 찾기 & 둘러보기")
    ap.add_argument("folders", nargs="*", help="스캔할 사진 폴더 (여러 개 가능)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--all-files", action="store_true", help="사진/동영상 외의 모든 파일도 포함")
    ap.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")
    ap.add_argument("--no-scan", action="store_true", help="이전 스캔 결과만 열기")
    args = ap.parse_args()

    APP_DIR.mkdir(parents=True, exist_ok=True)
    if args.folders:
        if args.no_scan and open_existing(args.folders):
            print("이전 스캔 결과를 엽니다.")
        else:
            try:
                start_scan(args.folders, args.all_files)
            except ValueError as e:
                print(e)
                sys.exit(1)
    elif load_last() and open_existing(load_last()):
        print("이전 스캔 결과를 엽니다: %s" % ", ".join(STATE.roots))

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    url = "http://127.0.0.1:%d/" % args.port
    print("=" * 60)
    print(" 사진 정리 도우미가 실행되었습니다.")
    print(" 브라우저에서 열기: %s" % url)
    print(" 썸네일(Pillow): %s / HEIC: %s / 휴지통(send2trash): %s"
          % ("O" if HAS_PIL else "X", "O" if HAS_HEIF else "X", "O" if HAS_TRASH else "X"))
    print(" 종료하려면 이 창에서 Ctrl+C 를 누르세요.")
    print("=" * 60)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        STATE.cancel = True
        print("\n종료합니다.")


if __name__ == "__main__":
    main()
