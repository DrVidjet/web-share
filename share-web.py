#!/usr/bin/env python3
"""web-share: веб-файловый менеджер общей папки.

Загрузка, скачивание (папки - zip), создание папок, переименование, удаление,
копирование / вырезание / вставка, просмотр содержимого архивов (zip, tar, tar.gz/bz2/xz, gz),
скачивание отдельного файла из архива, страница «поделиться». Только stdlib python3 (>= 3.7), Linux и Windows.
Настройки - .env рядом со скриптом (пишет start.sh / start.ps1, все ключи - в .env.example)
или переменные окружения SHARE_*.
"""
import hmac
import ipaddress
import html
import secrets
import threading
import json
import logging
import os
import sys
import shutil
import stat
import tarfile
import time
import urllib.parse
import zipfile
import zlib
import gzip
import hashlib
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

WIN = os.name == "nt"


def load_conf():
    """.env (KEY=VALUE, # - комментарий) рядом со скриптом (старое имя - config.env);
    переменные окружения SHARE_* важнее. Пустое значение = не задано."""
    conf = {}
    here = os.path.dirname(os.path.realpath(__file__))
    for path in ([os.environ["SHARE_CONF"]] if os.environ.get("SHARE_CONF") else
                 [os.path.join(here, ".env"), os.path.join(here, "config.env")]):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        if v.strip():
                            conf[k.strip()] = v.strip()
            break
        except OSError:
            continue
    conf.update((k, v) for k, v in os.environ.items() if k.startswith("SHARE_"))
    return conf


C = load_conf()


def setup_logging(level, path=""):
    """Уровень: ERROR / WARNING / INFO / DEBUG. Вывод - stderr (systemd пишет его в файл лога или журнал;
    в журнал - без времени, его journald ставит сам) или файл path с ротацией (Windows: 20 МБ, хранит 8)."""
    lvl = logging.getLevelName(str(level).strip().upper())
    fmt = "%(levelname)s %(message)s" if os.environ.get("JOURNAL_STREAM") else "%(asctime)s %(levelname)s %(message)s"
    handlers = None
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        handlers = [RotatingFileHandler(path, maxBytes=20 << 20, backupCount=8, encoding="utf-8")]
    logging.basicConfig(level=lvl if isinstance(lvl, int) else logging.INFO, format=fmt, datefmt="%Y-%m-%d %H:%M:%S",
                        handlers=handlers)
    if not isinstance(lvl, int):
        logging.warning("SHARE_LOG_LEVEL=%s - нет такого уровня (ERROR/WARNING/INFO/DEBUG), взят INFO", level)


setup_logging(C.get("SHARE_LOG_LEVEL", "INFO"), C.get("SHARE_LOG_FILE", ""))
log = logging.getLogger("share-web")
ROOT = os.path.realpath(C.get("SHARE_ROOT", r"C:\Share" if WIN else "/srv/share"))
ROOT_SEP = ROOT if ROOT.endswith(os.sep) else ROOT + os.sep  # корень диска (D:\\) уже с разделителем
BIND = C.get("SHARE_BIND", "0.0.0.0")
PORT = int(C.get("SHARE_PORT", "8081"))
# с каких адресов можно открыть страницу (CIDR через запятую)
ALLOW = [ipaddress.ip_network(n.strip(), strict=False)
         for n in C.get("SHARE_ALLOW", "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8").split(",") if n.strip()]
TITLE = C.get("SHARE_TITLE") or os.path.basename(ROOT) or "share"
# имена в корне общей папки, которых для веба как бы нет (lost+found, если корень - сам раздел;
# в Windows - служебные папки корня диска). В Windows имена без учёта регистра.
HIDE = {x.strip().casefold() if WIN else x.strip()
        for x in C.get("SHARE_HIDE", "lost+found,$RECYCLE.BIN,System Volume Information" if WIN else "lost+found")
        .split(",") if x.strip()}
SMB_PATHS = [x.strip() for x in C.get("SHARE_SMB_PATHS", "").split(",") if x.strip()]  # только для показа в шапке
CHUNK = 1024 * 1024
# вход для изменения файлов (без входа - только просмотр и скачивание)
ADMIN_USER = C.get("SHARE_USER", "admin")
ADMIN_PASS = C.get("SHARE_PASS", "")
# upload: читать/скачивать всё, загружать новые файлы и создавать папки только внутри UPLOAD_DIR;
# без переименования, удаления, копирования/перемещения и перезаписи существующих файлов. Пустой логин - выключено.
UPLOAD_USER = C.get("SHARE_UPLOAD_USER", "")
UPLOAD_PASS = C.get("SHARE_UPLOAD_PASS", "")
UPLOAD_DIR = os.path.normpath(os.path.join(ROOT, C.get("SHARE_UPLOAD_DIR", "upload").strip("/\\")))
USERS = {}
if UPLOAD_USER and UPLOAD_PASS:
    USERS[UPLOAD_USER] = (UPLOAD_PASS, "upload")
if ADMIN_USER and ADMIN_PASS:
    USERS[ADMIN_USER] = (ADMIN_PASS, "admin")
SESSION_TTL = int(float(C.get("SHARE_SESSION_HOURS", "12")) * 3600)
SESSIONS = {}  # токен -> [время истечения, роль, логин]
SLOCK = threading.Lock()
ARCH_LIMIT = 20000
# комментарии к файлам/папкам: {путь от корня: текст}. По умолчанию - StateDirectory службы (/var/lib/<служба>)
STATE_DIR = os.environ.get("STATE_DIRECTORY", "").split(os.pathsep)[0] or os.path.dirname(os.path.realpath(__file__))
COMMENTS_FILE = C.get("SHARE_COMMENTS_FILE") or os.path.join(STATE_DIR, "comments.json")
COMMENT_MAX = 1000
CLOCK = threading.Lock()


def comments_load():
    try:
        with open(COMMENTS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return {str(k): str(v) for k, v in d.items() if v} if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("комментарии не прочитать (%s): %s", COMMENTS_FILE, e)
        return {}


COMMENTS = comments_load()


def comments_save():
    """Вызывать под CLOCK."""
    tmp = COMMENTS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(COMMENTS, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, COMMENTS_FILE)
    except OSError as e:
        raise Err(500, "Не сохранить комментарии (%s): %s" % (COMMENTS_FILE, e))


def comments_under(rel):
    """Ключи комментариев самого rel и всего, что внутри него."""
    return [k for k in COMMENTS if k == rel or k.startswith(rel + "/")]


def comments_move(src_rel, dst_rel, copy=False):
    """Комментарии вслед за переименованием/перемещением/копированием (вместе с содержимым папки)."""
    with CLOCK:
        keys = comments_under(src_rel)
        if not keys:
            return
        for k in keys:
            v = COMMENTS[k] if copy else COMMENTS.pop(k)
            COMMENTS[dst_rel + k[len(src_rel):]] = v
        comments_save()


def comments_drop(rel):
    with CLOCK:
        keys = comments_under(rel)
        for k in keys:
            del COMMENTS[k]
        if keys:
            comments_save()


class Err(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def hidden(name):
    """Имя в корне общей папки, скрытое SHARE_HIDE."""
    return (name.casefold() if WIN else name) in HIDE


def resolve(rel, must_exist=True):
    rel = rel or ""
    if WIN:
        # в Windows "\\" - тоже разделитель; ":" - диск ("C:x") или поток NTFS ("файл:поток") - не пускаем
        rel = rel.replace("\\", "/")
        if ":" in rel:
            raise Err(400, "Недопустимый путь: " + rel)
    rel = rel.strip("/")
    if hidden(rel.split("/", 1)[0]):
        raise Err(404, "Не найдено: /" + rel)
    p = os.path.realpath(os.path.join(ROOT, rel))
    if p != ROOT and not p.startswith(ROOT_SEP):
        raise Err(403, "Путь вне общей папки")
    # ещё раз - по настоящему имени: ссылка на скрытое, "LOST+FOUND" и "lost+found." в Windows
    if p != ROOT and hidden(relpath(p).split("/", 1)[0]):
        raise Err(404, "Не найдено: /" + rel)
    if must_exist and not os.path.lexists(p):
        raise Err(404, "Не найдено: /" + rel)
    return p


def relpath(p):
    """Путь от корня общей папки, всегда через "/" (и в Windows)."""
    r = os.path.relpath(p, ROOT)
    return "" if r == "." else r.replace(os.sep, "/")


WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"%s%d" % (d, i) for d in ("COM", "LPT") for i in range(1, 10)}


def check_name(name):
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise Err(400, "Недопустимое имя: %r" % name)
    if WIN and (any(c in name for c in '\\:*?"<>|') or any(ord(c) < 32 for c in name) or name[-1] in ". "
                or name.split(".", 1)[0].rstrip(" ").upper() in WIN_RESERVED):
        raise Err(400, "Недопустимое имя для Windows: %r" % name)
    return name


def unique_dest(d, name):
    """Имя для вставки: если занято - 'имя (копия)', 'имя (копия 2)'..."""
    dst = os.path.join(d, name)
    if not os.path.lexists(dst):
        return dst
    base, ext = name, ""
    if not os.path.isdir(dst) and "." in name.lstrip("."):
        i = name.rfind(".")
        if name[:i].endswith(".tar"):
            i = name[:i].rfind(".")
        base, ext = name[:i], name[i:]
    n = 1
    while True:
        cand = "%s (копия%s)%s" % (base, "" if n == 1 else " %d" % n, ext)
        dst = os.path.join(d, cand)
        if not os.path.lexists(dst):
            return dst
        n += 1


def dir_size(path):
    """Размер папки со всем содержимым (ссылки не раскрываются)."""
    total = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for de in it:
                    try:
                        if de.is_dir(follow_symlinks=False):
                            stack.append(de.path)
                        else:
                            total += de.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def hsize(n):
    for u in ("байт", "КБ", "МБ", "ГБ"):
        if n < 1024 or u == "ГБ":
            return ("%d %s" if u == "байт" else "%.1f %s") % (n, u)
        n /= 1024.0


# ---------- папка -> zip: размер известен заранее, докачка (Range) ----------
# CRC файлов, уже отданных в zip: ключ "dev:ino:размер:mtime_ns" -> crc32. Нужен, чтобы при докачке с середины
# не перечитывать с диска всё, что было до места обрыва (CRC пишутся после данных и в каталоге в конце).
CRC_FILE = os.path.join(STATE_DIR, "zip-crc.json")
CRC_MAX = 100000
CRC_LOCK = threading.Lock()
CRC_BUSY = {}  # ключ -> Lock: один файл не считаем в два потока
ZIP64_LIMIT = (1 << 31) - 1  # как в zipfile: больше - ZIP64


def crc_load():
    try:
        with open(CRC_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return {str(k): int(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("кэш CRC для zip не прочитать (%s): %s", CRC_FILE, e)
        return {}


CRC_CACHE = crc_load()
CRC_DIRTY = [False]


def crc_put(key, crc):
    with CRC_LOCK:
        CRC_CACHE.pop(key, None)
        CRC_CACHE[key] = crc
        while len(CRC_CACHE) > CRC_MAX:
            del CRC_CACHE[next(iter(CRC_CACHE))]
        CRC_DIRTY[0] = True


def crc_save():
    with CRC_LOCK:
        if not CRC_DIRTY[0]:
            return
        CRC_DIRTY[0] = False
        tmp = CRC_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(CRC_CACHE, f)
            os.replace(tmp, CRC_FILE)
        except OSError as e:
            log.warning("кэш CRC для zip не сохранить (%s): %s", CRC_FILE, e)


def read_exact(f, n, e):
    """n байт файла; если файл укоротился (или не открылся) - добиваем нулями, чтобы не сбить разметку zip."""
    b = f.read(n) if f else b""
    if len(b) < n:
        if not e.get("warned"):
            e["warned"] = True
            log.warning("zip: файл %s изменился или не читается во время отдачи - в архиве будет испорчен",
                        e["path"])
        b += b"\0" * (n - len(b))
    return b


def open_or_none(e):
    try:
        return open(e["path"], "rb")
    except OSError as ex:
        if not e.get("warned"):
            e["warned"] = True
            log.warning("zip: %s не открыть: %s", e["path"], ex)
        return None


def crc_file(e, stop=None):
    """CRC файла (из кэша или прочитав его целиком). None - прервано через stop."""
    key = e["key"]
    with CRC_LOCK:
        if key in CRC_CACHE:
            return CRC_CACHE[key]
        lk = CRC_BUSY.setdefault(key, threading.Lock())
    with lk:
        with CRC_LOCK:
            if key in CRC_CACHE:
                return CRC_CACHE[key]
        c, left = 0, e["size"]
        f = open_or_none(e)
        try:
            while left > 0:
                if stop is not None and stop.is_set():
                    return None
                b = read_exact(f, min(CHUNK, left), e)
                c = zlib.crc32(b, c)
                left -= len(b)
        finally:
            if f:
                f.close()
        crc_put(key, c)
    with CRC_LOCK:
        CRC_BUSY.pop(key, None)
    return c


def dos_time(mtime):
    t = time.localtime(mtime)
    if t.tm_year < 1980:
        return 0, (1 << 5) | 1
    if t.tm_year > 2107:
        return (23 << 11) | (59 << 5) | 29, (127 << 9) | (12 << 5) | 31
    return (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2), ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday


class ZipPlan:
    """Папка -> zip без сжатия (ISO/архивы всё равно не жмутся), разложенный заранее: размер известен до отдачи,
    любой кусок (Range) при докачке собирается заново байт в байт, пока файлы в папке не менялись (ETag).
    CRC - в дескрипторе после данных (флаг 0x08, как пишет zipfile в поток) и в центральном каталоге,
    поэтому заголовки перед данными от CRC не зависят. ZIP64 - для файлов > 2 ГБ и всего, что дальше 2 ГБ от начала."""

    def __init__(self, p):
        base = os.path.basename(p) or "share"
        self.ents = []
        for d, dirs, files in os.walk(p):
            if d == ROOT:
                dirs[:] = [x for x in dirs if not hidden(x)]
                files = [x for x in files if not hidden(x)]
            dirs.sort()
            arc = (base + "/" + os.path.relpath(d, p).replace(os.sep, "/")) if d != p else base
            if not files and not dirs:
                try:
                    st = os.stat(d)
                except OSError:
                    continue
                self.add(arc + "/", None, st, True)
            for f in sorted(files):
                fp = os.path.join(d, f)
                try:
                    st = os.stat(fp)  # ссылки раскрываются, как раньше
                except OSError:
                    continue  # битые ссылки
                if stat.S_ISREG(st.st_mode) and os.access(fp, os.R_OK):  # сокеты, нечитаемое - мимо
                    self.add(arc + "/" + f, fp, st, False)
        # разметка: [локальный заголовок, данные, дескриптор]..., центральный каталог + концовка
        self.parts = []
        off = 0
        h = hashlib.sha1()
        for e in self.ents:
            e["off"] = off
            # ZIP64 и по смещению: unzip (Info-ZIP) по ZIP64-записи в каталоге ждёт 24-байтный дескриптор,
            # иначе - «overlapped components (possible zip bomb)»
            e["zip64"] = not e["dir"] and (e["size"] > ZIP64_LIMIT or off > ZIP64_LIMIT)
            lh = self.local(e)
            self.parts.append((off, len(lh), "b", lh))
            off += len(lh)
            if not e["dir"]:
                self.parts.append((off, e["size"], "f", e))
                off += e["size"]
                n = 24 if e["zip64"] else 16
                self.parts.append((off, n, "d", e))
                off += n
            h.update(e["name"] + b"\0" + e["key"].encode() + b"\0")
        self.cd_off = off
        self.cd_len = len(self.central(lambda e: 0))
        self.parts.append((off, self.cd_len, "c", None))
        self.size = off + self.cd_len
        self.files = sum(1 for e in self.ents if not e["dir"])
        self.etag = '"z%s-%x"' % (h.hexdigest()[:20], self.size)
        self.cd = None

    def add(self, name, path, st, isdir):
        tm, dt = dos_time(st.st_mtime)
        size = 0 if isdir else st.st_size
        self.ents.append({
            "name": name.encode("utf-8", "surrogateescape"), "path": path, "dir": isdir, "size": size,
            "tm": tm, "dt": dt,
            "attr": ((0o40000 | (st.st_mode & 0o7777)) << 16 | 0x10) if isdir else (st.st_mode & 0xFFFF) << 16,
            "key": "%d:%d:%d:%d" % (st.st_dev, st.st_ino, size, st.st_mtime_ns)})

    @staticmethod
    def flags(e):
        return (0 if e["dir"] else 0x08) | (0 if e["name"].isascii() else 0x800)

    def local(self, e):
        extra = struct.pack("<HHQQ", 1, 16, 0, 0) if e["zip64"] else b""
        sz = 0xFFFFFFFF if e["zip64"] else 0
        return struct.pack("<4s2B4HL2L2H", b"PK\003\004", 45 if e["zip64"] else 20, 0, self.flags(e), 0,
                           e["tm"], e["dt"], 0, sz, sz, len(e["name"]), len(extra)) + e["name"] + extra

    @staticmethod
    def descriptor(e, crc):
        return struct.pack("<LLQQ" if e["zip64"] else "<LLLL", 0x08074b50, crc, e["size"], e["size"])

    def central(self, crc_of):
        out = []
        for e in self.ents:
            vals = []
            size, off = e["size"], e["off"]
            if size > ZIP64_LIMIT:
                vals += [size, size]
                size = 0xFFFFFFFF
            if off > ZIP64_LIMIT:
                vals.append(off)
                off = 0xFFFFFFFF
            extra = struct.pack("<HH%dQ" % len(vals), 1, 8 * len(vals), *vals) if vals else b""
            ver = 45 if vals or e["zip64"] else 20
            crc = 0 if e["dir"] else crc_of(e)
            out.append(struct.pack("<4s4B4HL2L5HLL", b"PK\001\002", ver, 3, ver, 0, self.flags(e), 0,
                                   e["tm"], e["dt"], crc, size, size, len(e["name"]), len(extra), 0, 0, 0,
                                   e["attr"], off) + e["name"] + extra)
        cd = b"".join(out)
        n, cd_size, cd_off = len(self.ents), len(cd), self.cd_off
        if n >= 0xFFFF or cd_size > ZIP64_LIMIT or cd_off > ZIP64_LIMIT:
            cd += struct.pack("<4sQ2H2L4Q", b"PK\006\006", 44, 45, 45, 0, 0, n, n, cd_size, cd_off)
            cd += struct.pack("<4sLQL", b"PK\006\007", 0, cd_off + cd_size, 1)
            n, cd_size, cd_off = min(n, 0xFFFF), min(cd_size, 0xFFFFFFFF), min(cd_off, 0xFFFFFFFF)
        return cd + struct.pack("<4s4H2LH", b"PK\005\006", 0, 0, n, n, cd_size, cd_off, 0)

    def chunks(self, start, end):
        """Байты архива [start, end] кусками. CRC файлов, лежащих (хоть частично) до start, считаются в фоне -
        они понадобятся в дескрипторах и каталоге; файл, отдаваемый с начала, считается на лету."""
        stop = threading.Event()
        ahead = [e for e in self.ents if not e["dir"] and e["off"] < start and e["key"] not in CRC_CACHE]
        if ahead and end >= self.cd_off:
            threading.Thread(target=lambda: [crc_file(e, stop) for e in ahead if not stop.is_set()],
                             daemon=True).start()
        try:
            for o, n, kind, x in self.parts:
                if n == 0 or o + n <= start:
                    continue
                if o > end:
                    break
                a, b = max(start, o) - o, min(end + 1, o + n) - o
                if kind == "b":
                    yield x[a:b]
                elif kind == "f":
                    for c in self.file_part(x, a, b):
                        yield c
                elif kind == "d":
                    yield self.descriptor(x, crc_file(x))[a:b]
                else:
                    if self.cd is None:
                        self.cd = self.central(crc_file)
                    yield self.cd[a:b]
        finally:
            stop.set()
            crc_save()

    def file_part(self, e, a, b):
        track = a == 0 and e["key"] not in CRC_CACHE  # файл с начала - CRC заодно
        c = 0
        f = open_or_none(e)
        try:
            if f and a:
                f.seek(a)
            pos = a
            while pos < b:
                d = read_exact(f, min(CHUNK, b - pos), e)
                if track:
                    c = zlib.crc32(d, c)
                pos += len(d)
                yield d
        finally:
            if f:
                f.close()
        if track and b == e["size"]:
            crc_put(e["key"], c)


def parse_range(hdr, size):
    """Заголовок Range (один диапазон) -> (start, end) или None (весь файл); ValueError - вне файла (416)."""
    if not hdr or not hdr.startswith("bytes=") or size <= 0:
        return None
    a, _, b = hdr[6:].split(",")[0].strip().partition("-")
    try:
        if a:
            start, end = int(a), (int(b) if b else size - 1)
        elif b:
            start, end = max(0, size - int(b)), size - 1
        else:
            return None
    except ValueError:
        return None
    end = min(end, size - 1)
    if start > end:
        raise ValueError
    return start, end


def archive_kind(path):
    n = path.lower()
    if zipfile.is_zipfile(path):
        return "zip"
    try:
        if tarfile.is_tarfile(path):
            return "tar"
    except Exception:
        pass
    if n.endswith(".gz"):
        return "gz"
    return None


def list_archive(path):
    kind = archive_kind(path)
    items = []
    truncated = False
    if kind == "zip":
        with zipfile.ZipFile(path) as z:
            for i in z.infolist():
                if len(items) >= ARCH_LIMIT:
                    truncated = True
                    break
                items.append({"name": i.filename, "dir": i.is_dir(), "size": i.file_size,
                              "mtime": time.mktime(i.date_time + (0, 0, -1))})
    elif kind == "tar":
        # потоковое чтение: для сжатых tar не требует распаковки на диск
        with tarfile.open(path, "r|*") as t:
            for m in t:
                if len(items) >= ARCH_LIMIT:
                    truncated = True
                    break
                items.append({"name": m.name + ("/" if m.isdir() else ""), "dir": m.isdir(),
                              "size": m.size, "mtime": m.mtime})
    elif kind == "gz":
        name = os.path.basename(path)[:-3]
        with gzip.open(path, "rb") as g:
            size = 0
            while True:
                b = g.read(CHUNK)
                if not b:
                    break
                size += len(b)
        items.append({"name": name, "dir": False, "size": size, "mtime": os.path.getmtime(path)})
    else:
        raise Err(400, "Не архив или формат не поддерживается")
    return {"kind": kind, "items": items, "truncated": truncated}


class H(BaseHTTPRequestHandler):
    server_version = "share-web/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        log.debug("%s %s", self.client_address[0], fmt % a)

    def who(self):
        """«ip логин» для лога действий."""
        t = self.token()
        with SLOCK:
            s = SESSIONS.get(t) if t else None
        return "%s %s" % (self.client_address[0], s[2] if s else "гость")

    # ---------- helpers ----------
    def allowed(self):
        try:
            a = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        if a.version == 6 and a.ipv4_mapped:
            a = a.ipv4_mapped
        return any(a in n for n in ALLOW)

    def token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sw_sess":
                return v
        return ""

    def role(self):
        """None (гость), "admin" или "upload"."""
        t = self.token()
        if not t:
            return None
        with SLOCK:
            s = SESSIONS.get(t)
            if s and s[0] > time.time():
                s[0] = time.time() + SESSION_TTL
                return s[1]
            SESSIONS.pop(t, None)
        return None

    def is_admin(self):
        return self.role() == "admin"

    def need_admin(self):
        r = self.role()
        if r is None:
            raise Err(401, "Требуется вход (кнопка «Войти»)")
        if r != "admin":
            raise Err(403, "Пользователю %s это запрещено" % UPLOAD_USER)

    def need_write(self, path):
        """Можно ли создавать новое в path: admin - везде, upload - только внутри UPLOAD_DIR."""
        r = self.role()
        if r == "admin":
            return r
        if r is None:
            raise Err(401, "Требуется вход (кнопка «Войти»)")
        if path != UPLOAD_DIR and not path.startswith(UPLOAD_DIR + os.sep):
            raise Err(403, "Пользователю %s можно загружать только в папку /%s" % (UPLOAD_USER, relpath(UPLOAD_DIR)))
        return r

    def login(self, d):
        user, pw = str(d.get("user", "")), str(d.get("pass", ""))
        ok, role = False, None
        for u, (p, r) in USERS.items():
            if hmac.compare_digest(user.encode(), u.encode()) & hmac.compare_digest(pw.encode(), p.encode()):
                ok, role = True, r
        if not ok:
            log.warning("%s: неудачный вход, логин «%s»", self.client_address[0], user[:64])
            time.sleep(1)
            raise Err(403, "Неверный логин или пароль")
        t = secrets.token_urlsafe(32)
        now = time.time()
        with SLOCK:
            for k in [k for k, v in SESSIONS.items() if v[0] < now]:
                del SESSIONS[k]
            SESSIONS[t] = [now + SESSION_TTL, role, user]
        log.info("%s: вход %s (%s)", self.client_address[0], user, role)
        self.send_json({"ok": True, "admin": role == "admin", "role": role},
                       cookie="sw_sess=%s; Path=/; HttpOnly; SameSite=Strict" % t)

    def logout(self):
        log.info("[%s] выход", self.who())
        with SLOCK:
            SESSIONS.pop(self.token(), None)
        self.send_json({"ok": True, "admin": False},
                       cookie="sw_sess=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")

    def q(self):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def qget(self, k, d=""):
        return self.q().get(k, [d])[0]

    def send_json(self, obj, code=200, cookie=None):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 10 * 1024 * 1024:
            raise Err(413, "Слишком большой запрос")
        return json.loads(self.rfile.read(n) or b"{}")

    def disp(self, name):
        return "attachment; filename*=UTF-8''" + urllib.parse.quote(name)

    def handle_err(self, e):
        if isinstance(e, Err):
            code, msg = e.code, e.msg
        elif isinstance(e, PermissionError):
            code, msg = 403, "Нет прав: %s" % e
        elif isinstance(e, FileNotFoundError):
            code, msg = 404, "Не найдено: %s" % e
        elif isinstance(e, FileExistsError):
            code, msg = 409, "Уже существует: %s" % os.path.basename(e.filename or "")
        else:
            code, msg = 500, "%s: %s" % (type(e).__name__, e)
        route = urllib.parse.urlsplit(self.path).path
        if route == "/api/login" and code == 403:
            pass  # неудачный вход уже записан в login()
        elif code >= 500:
            log.error("[%s] %s %s: %s", self.who(), self.command, route, msg, exc_info=e)
        elif code in (401, 403):
            log.warning("[%s] %s %s: отказ %d: %s", self.who(), self.command, route, code, msg)
        else:
            log.info("[%s] %s %s: %d: %s", self.who(), self.command, route, code, msg)
        # тело запроса (загрузка) могло остаться непрочитанным - соединение не переиспользуем
        self.close_connection = True
        try:
            self.send_json({"error": msg}, code)
        except Exception:
            pass

    # ---------- routing ----------
    def deny(self):
        log.warning("%s: адрес не из SHARE_ALLOW, %s %s - 403", self.client_address[0], self.command, self.path[:200])
        self.send_error(403)

    def do_GET(self):
        if not self.allowed():
            return self.deny()
        route = urllib.parse.urlsplit(self.path).path
        try:
            if route == "/":
                b = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(b)
            elif route == "/api/version":
                self.send_json({"build": BUILD})
            elif route == "/api/me":
                r = self.role()
                self.send_json({"admin": r == "admin", "role": r, "user": ADMIN_USER,
                                "upload_dir": relpath(UPLOAD_DIR)})
            elif route == "/api/list":
                self.api_list()
            elif route == "/api/info":
                self.api_info()
            elif route.startswith("/s/") or route == "/s":
                b = SHARE_PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)
            elif route == "/api/archive":
                self.send_json(list_archive(resolve(self.qget("path"))))
            elif route == "/dl":
                self.download()
            elif route == "/api/archive_get":
                self.archive_get()
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError) as e:
            log.debug("%s: соединение оборвано: %s", self.client_address[0], e)
        except Exception as e:
            self.handle_err(e)

    def do_PUT(self):
        if not self.allowed():
            return self.deny()
        try:
            if urllib.parse.urlsplit(self.path).path == "/api/upload":
                self.upload()
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError) as e:
            log.debug("%s: соединение оборвано: %s", self.client_address[0], e)
        except Exception as e:
            self.handle_err(e)

    def do_POST(self):
        if not self.allowed():
            return self.deny()
        route = urllib.parse.urlsplit(self.path).path
        try:
            d = self.body_json()
            if route == "/api/login":
                return self.login(d)
            if route == "/api/logout":
                return self.logout()
            if route in ("/api/rename", "/api/delete", "/api/paste", "/api/comment"):
                self.need_admin()
            if route == "/api/exists":
                return self.api_exists(d)
            if route == "/api/mkdir":
                parent = resolve(d.get("path"))
                self.need_write(parent)
                p = os.path.join(parent, check_name(d.get("name", "").strip()))
                os.mkdir(p)
                log.info("[%s] создана папка %s", self.who(), relpath(p))
                self.send_json({"ok": True})
            elif route == "/api/rename":
                src = resolve(d.get("path"))
                if src == ROOT:
                    raise Err(400, "Нельзя переименовать корень")
                dst = os.path.join(os.path.dirname(src), check_name(d.get("name", "").strip()))
                if os.path.lexists(dst):
                    raise Err(409, "Уже существует: " + os.path.basename(dst))
                os.rename(src, dst)
                log.info("[%s] переименовано: %s -> %s", self.who(), relpath(src), os.path.basename(dst))
                comments_move(relpath(src), relpath(dst))
                self.send_json({"ok": True})
            elif route == "/api/delete":
                for r in d.get("paths", []):
                    p = resolve(r)
                    if p == ROOT:
                        raise Err(400, "Нельзя удалить корень")
                    if os.path.isdir(p) and not os.path.islink(p):
                        shutil.rmtree(p)
                        log.info("[%s] удалена папка %s", self.who(), relpath(p))
                    else:
                        os.remove(p)
                        log.info("[%s] удалён %s", self.who(), relpath(p))
                    comments_drop(relpath(p))
                self.send_json({"ok": True})
            elif route == "/api/paste":
                self.paste(d)
            elif route == "/api/comment":
                p = resolve(d.get("path"))
                if p == ROOT:
                    raise Err(400, "Корню комментарий не нужен")
                text = " ".join(str(d.get("text", "")).split())[:COMMENT_MAX]
                with CLOCK:
                    if text:
                        COMMENTS[relpath(p)] = text
                    else:
                        COMMENTS.pop(relpath(p), None)
                    comments_save()
                log.info("[%s] комментарий к %s: «%s»", self.who(), relpath(p), text)
                self.send_json({"ok": True, "text": text})
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError) as e:
            log.debug("%s: соединение оборвано: %s", self.client_address[0], e)
        except Exception as e:
            self.handle_err(e)

    # ---------- actions ----------
    def api_list(self):
        p = resolve(self.qget("path"))
        if not os.path.isdir(p):
            raise Err(400, "Не папка")
        items = []
        base = relpath(p)
        with os.scandir(p) as it:
            for de in it:
                if not base and hidden(de.name):
                    continue
                try:
                    st = de.stat(follow_symlinks=True)
                    isdir = stat.S_ISDIR(st.st_mode)
                    item = {"name": de.name, "dir": isdir,
                            "size": dir_size(de.path) if isdir else st.st_size, "mtime": st.st_mtime}
                except OSError:
                    item = {"name": de.name, "dir": False, "size": 0, "mtime": 0}
                item["comment"] = COMMENTS.get(base + "/" + de.name if base else de.name, "")
                items.append(item)
        du = shutil.disk_usage(p)
        self.send_json({"path": relpath(p), "items": items,
                        "free": du.free, "total": du.total})

    def api_info(self):
        """Карточка для страницы «поделиться»: имя, тип, размер, дата (для папки - ещё число файлов)."""
        p = resolve(self.qget("path"))
        st = os.stat(p)
        isdir = stat.S_ISDIR(st.st_mode)
        info = {"path": relpath(p), "name": os.path.basename(p) or "share", "dir": isdir,
                "mtime": st.st_mtime, "size": st.st_size, "comment": COMMENTS.get(relpath(p), "")}
        if isdir:
            files = dirs = 0
            for _, ds, fs in os.walk(p):
                dirs += len(ds)
                files += len(fs)
            info.update(size=dir_size(p), files=files, dirs=dirs)
        self.send_json(info)

    def download(self):
        p = resolve(self.qget("path"))
        if os.path.isdir(p):
            return self.send_zip(p)
        size = os.path.getsize(p)
        try:
            r = parse_range(self.headers.get("Range"), size)
        except ValueError:
            return self.send_416(size)
        start, end = r or (0, size - 1)
        length = end - start + 1 if size else 0
        # докачка кусками (Range) - одна запись на начало файла, остальные куски - DEBUG
        log.log(logging.INFO if start == 0 else logging.DEBUG, "[%s] скачивание %s (%s%s)", self.who(),
                relpath(p), hsize(size), ", с %s" % hsize(start) if start else "")
        self.send_response(206 if r else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", self.disp(os.path.basename(p)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if r:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()

        def chunks():
            with open(p, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    b = f.read(min(CHUNK, left))
                    if not b:
                        break
                    yield b
                    left -= len(b)
        self.pump(chunks(), relpath(p), start, length, size)

    def send_416(self, size):
        self.send_response(416)
        self.send_header("Content-Range", "bytes */%d" % size)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_zip(self, p):
        """Папка -> zip (ZipPlan): Content-Length известен, докачка по Range с проверкой If-Range (ETag)."""
        plan = ZipPlan(p)
        rel = relpath(p) or "/"
        rng = self.headers.get("Range")
        ifr = self.headers.get("If-Range")
        if ifr and ifr.strip() != plan.etag:
            rng = None  # папка изменилась с прошлого раза - отдаём заново целиком
        try:
            r = parse_range(rng, plan.size)
        except ValueError:
            return self.send_416(plan.size)
        start, end = r or (0, plan.size - 1)
        log.info("[%s] скачивание папки (zip) %s (%s, файлов %d%s)", self.who(), rel, hsize(plan.size), plan.files,
                 ", докачка с %s" % hsize(start) if start else "")
        self.send_response(206 if r else 200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", self.disp((os.path.basename(p) or "share") + ".zip"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", plan.etag)
        self.send_header("Content-Length", str(end - start + 1))
        if r:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, plan.size))
        self.end_headers()
        if self.pump(plan.chunks(start, end), rel + " (zip)", start, end - start + 1, plan.size) and end == plan.size - 1:
            log.info("[%s] папка (zip) %s отдана до конца", self.who(), rel)

    def pump(self, chunks, what, start, length, size):
        """Отдать куски; обрыв клиентом - в лог (INFO) с тем, сколько успело уйти. True - отдано всё."""
        sent = 0
        t0 = time.time()
        try:
            for b in chunks:
                self.wfile.write(b)
                sent += len(b)
            return True
        except (BrokenPipeError, ConnectionResetError, TimeoutError) as e:
            self.close_connection = True
            log.info("[%s] скачивание прервано: %s - отдано %s из %s%s за %d с (%s)", self.who(), what,
                     hsize(sent), hsize(length), ", начиная с %s из %s" % (hsize(start), hsize(size)) if start else "",
                     time.time() - t0, e.strerror or type(e).__name__)
            return False
        finally:
            close = getattr(chunks, "close", None)
            if close:
                close()

    def archive_get(self):
        p = resolve(self.qget("path"))
        member = self.qget("member")
        kind = archive_kind(p)
        log.info("[%s] скачивание из архива %s: %s", self.who(), relpath(p), member)
        name = os.path.basename(member.rstrip("/")) or "file"
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", self.disp(name))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if kind == "zip":
            with zipfile.ZipFile(p) as z, z.open(member) as f:
                shutil.copyfileobj(f, self.wfile, CHUNK)
        elif kind == "tar":
            with tarfile.open(p, "r|*") as t:
                for m in t:
                    if m.name == member.rstrip("/") and m.isfile():
                        shutil.copyfileobj(t.extractfile(m), self.wfile, CHUNK)
                        break
        elif kind == "gz":
            with gzip.open(p, "rb") as f:
                shutil.copyfileobj(f, self.wfile, CHUNK)

    def api_exists(self, d):
        """Какие из names (пути от path, могут быть с подпапками) уже существуют - перед загрузкой."""
        base = resolve(d.get("path"))
        out = []
        for name in d.get("names", []):
            parts = [x for x in str(name).split("/") if x]
            if not parts or any(x in (".", "..") or "\0" in x for x in parts):
                continue
            p = os.path.join(base, *parts)
            if os.path.realpath(p).startswith(ROOT_SEP) and os.path.lexists(p):
                out.append(name)
        self.send_json({"exists": out})

    def upload(self):
        d = resolve(self.qget("path"))
        if not os.path.isdir(d):
            raise Err(400, "Не папка")
        rel_name = self.qget("name").strip("/")
        parts = [check_name(x) for x in rel_name.split("/") if x]
        if not parts:
            raise Err(400, "Нет имени файла")
        dst = os.path.realpath(os.path.join(d, *parts))
        if not dst.startswith(ROOT_SEP):
            raise Err(403, "Путь вне общей папки")
        role = self.need_write(dst)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.lexists(dst) and (self.qget("overwrite") != "1" or role != "admin"):
            if role != "admin":
                raise Err(409, "Уже существует (замена запрещена): " + "/".join(parts))
            raise Err(409, "Уже существует: " + "/".join(parts))
        n = int(self.headers.get("Content-Length") or 0)
        tmp = os.path.join(os.path.dirname(dst), ".upload-%d-%s" % (os.getpid(), parts[-1]))
        try:
            with open(tmp, "wb") as f:
                left = n
                while left > 0:
                    b = self.rfile.read(min(CHUNK, left))
                    if not b:
                        raise Err(400, "Загрузка прервана")
                    f.write(b)
                    left -= len(b)
            replaced = os.path.lexists(dst)
            if role == "admin":
                os.replace(tmp, dst)
            elif WIN:
                os.rename(tmp, dst)  # в Windows rename не заменяет существующий файл (FileExistsError)
            else:
                os.link(tmp, dst)  # не заменит файл, появившийся за время загрузки (FileExistsError)
            log.info("[%s] загружен %s (%d байт%s)", self.who(), relpath(dst), n, ", заменён" if replaced else "")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        self.send_json({"ok": True})

    def paste(self, d):
        op = d.get("op")
        dest = resolve(d.get("dest"))
        if not os.path.isdir(dest):
            raise Err(400, "Назначение - не папка")
        for r in d.get("src", []):
            src = resolve(r)
            if src == ROOT:
                raise Err(400, "Нельзя копировать корень")
            if os.path.isdir(src) and (dest == src or dest.startswith(src + os.sep)):
                raise Err(400, "Нельзя вставить папку саму в себя")
            if op == "move":
                if os.path.dirname(src) == dest:
                    continue
                dst = os.path.join(dest, os.path.basename(src))
                if os.path.lexists(dst):
                    raise Err(409, "В папке назначения уже есть: " + os.path.basename(src))
                shutil.move(src, dst)
                log.info("[%s] перемещено: %s -> %s", self.who(), relpath(src), relpath(dst))
                comments_move(relpath(src), relpath(dst))
            elif op == "copy":
                dst = unique_dest(dest, os.path.basename(src))
                if os.path.isdir(src) and not os.path.islink(src):
                    shutil.copytree(src, dst, symlinks=True)
                else:
                    shutil.copy2(src, dst, follow_symlinks=False)
                log.info("[%s] скопировано: %s -> %s", self.who(), relpath(src), relpath(dst))
                comments_move(relpath(src), relpath(dst), copy=True)
            else:
                raise Err(400, "Неизвестная операция")
        self.send_json({"ok": True})


PAGE = r"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Общая папка share</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font-family:sans-serif;margin:0;background:#f4f6f8;color:#222}
header{background:#2c3e50;color:#fff;padding:10px 16px;font-size:18px}
header small{opacity:.75;font-size:13px;margin-left:10px}
.bar{display:flex;flex-wrap:wrap;gap:6px;padding:10px 16px;background:#fff;border-bottom:1px solid #ddd;position:sticky;top:0;z-index:2}
button{padding:6px 11px;border:1px solid #b9c2cc;background:#fff;border-radius:4px;cursor:pointer;font-size:14px}
button:hover:not(:disabled){background:#eaf2fb}
button:disabled{opacity:.45;cursor:default}
button.danger{border-color:#d9534f;color:#c9302c}
.crumbs{padding:10px 16px;font-size:15px}
.crumbs a{color:#2a6ebb;text-decoration:none;cursor:pointer}
.crumbs a:hover{text-decoration:underline}
#clip{padding:0 16px 8px;color:#8a6d3b;font-size:13px}
table{border-collapse:collapse;width:calc(100% - 32px);margin:0 16px 16px;background:#fff}
th,td{padding:6px 8px;border-bottom:1px solid #eee;text-align:left;font-size:14px}
th{background:#fafafa;cursor:pointer;user-select:none}
tr.sel td{background:#dbeafe}
tr:hover td{background:#f0f6ff}
tr.sel:hover td{background:#cfe2fc}
td.n{cursor:pointer;color:#1d4f8f}
td.n:hover{text-decoration:underline}
td.r,th.r{text-align:right;white-space:nowrap}
.ic{display:inline-block;width:20px}
#drop{display:none;position:fixed;inset:0;background:rgba(42,110,187,.15);border:4px dashed #2a6ebb;z-index:9;align-items:center;justify-content:center;font-size:28px;color:#2a6ebb}
#up{margin:0 16px 10px}
.upi{font-size:13px;margin:3px 0;padding:8px 10px;background:#fff;border:1px solid #ddd;border-radius:4px}
.upi .pb{margin-top:6px}
.upc{color:#666;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.upx{padding:1px 8px;font-size:12px;margin-left:6px}
.upi details{margin-top:6px;max-height:160px;overflow:auto}
.pb{height:6px;background:#e5e7eb;border-radius:3px;overflow:hidden}
.pb div{height:100%;background:#2a6ebb;width:0}
#modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:10;align-items:center;justify-content:center}
#modal .w{background:#fff;width:min(1000px,94vw);max-height:86vh;display:flex;flex-direction:column;border-radius:6px}
#modal .h{padding:10px 14px;border-bottom:1px solid #ddd;display:flex;justify-content:space-between;align-items:center;gap:10px}
#modal .b{overflow:auto;padding:0 0 10px}
#modal table{width:100%;margin:0}
#st{padding:4px 16px;font-size:13px;color:#555}
body.ro .rw{display:none}
body:not(.cw) .w{display:none}
body.anon .lo{display:none}
#who{margin-left:auto;align-self:center;font-size:13px;color:#555}
#cmodal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:11;align-items:center;justify-content:center}
#cmodal .w{background:#fff;padding:18px 20px;border-radius:6px;width:min(560px,94vw);display:flex;flex-direction:column;gap:10px}
#cml{max-height:50vh;overflow:auto;font-size:13px;border:1px solid #ddd;border-radius:4px;padding:6px 8px;word-break:break-all}
#cmodal .row{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap}
#lmodal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:11;align-items:center;justify-content:center}
#lmodal form{background:#fff;padding:18px 20px;border-radius:6px;width:300px;display:flex;flex-direction:column;gap:9px}
#lmodal input{padding:7px;font-size:14px;border:1px solid #b9c2cc;border-radius:4px}
#lmodal .row{display:flex;gap:8px;justify-content:flex-end}
.err{color:#c9302c}
td.cm{color:#555;max-width:40vw;overflow-wrap:anywhere}
body:not(.ro) td.cm{cursor:text}
body:not(.ro) td.cm:hover{background:#fffbe6}
body:not(.ro) td.cm:empty::before{content:"✎";opacity:0;color:#999}
body:not(.ro) tr:hover td.cm:empty::before{opacity:.6}
td.cm input{width:100%;box-sizing:border-box;font-size:14px;padding:3px 5px;border:1px solid #2a6ebb;border-radius:3px}
td.sh,th.sh{width:30px;text-align:center;padding:2px 0}
.shb{border:none;background:transparent;padding:3px 6px;border-radius:6px;font-size:15px;opacity:.6;transition:opacity .15s,background .15s}
tr:hover .shb{opacity:.85}
.shb:hover{opacity:1!important;background:#dbeafe!important}
#toast{position:fixed;left:50%;bottom:28px;transform:translate(-50%,30px);opacity:0;pointer-events:none;
 background:#1f2937;color:#fff;padding:11px 20px;border-radius:10px;font-size:14px;box-shadow:0 8px 24px rgba(0,0,0,.25);
 transition:opacity .25s,transform .25s;z-index:20;display:flex;gap:10px;align-items:center;max-width:90vw}
#toast.show{opacity:1;transform:translate(-50%,0)}
#toast .tl{color:#93c5fd;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:46vw}
</style></head><body class="ro anon">
<header>Общая папка <b>%%TITLE%%</b><small>%%SMB%%</small></header>
<div class="bar">
 <button id="bUp" class="w">⬆ Загрузить файлы</button>
 <button id="bUpD" class="w">⬆ Загрузить папку</button>
 <button id="bMk" class="w">📁 Новая папка</button>
 <button id="bDl" disabled>⬇ Скачать</button>
 <button id="bRn" class="rw" disabled>✎ Переименовать</button>
 <button id="bCp" class="rw" disabled>⧉ Копировать</button>
 <button id="bCt" class="rw" disabled>✂ Вырезать</button>
 <button id="bPs" class="rw" disabled>📋 Вставить</button>
 <button id="bAr" disabled>🗜 Содержимое архива</button>
 <button id="bRm" class="danger rw" disabled>🗑 Удалить</button>
 <button id="bRf">⟳ Обновить</button>
 <span id="who">Гость: только просмотр и скачивание</span>
 <button id="bLogin">🔑 Войти</button>
 <button id="bLogout" class="lo">Выйти</button>
</div>
<div class="crumbs" id="crumbs"></div>
<div id="clip"></div>
<div id="up"></div>
<table><thead><tr><th style="width:24px"><input type="checkbox" id="all"></th><th class="sh" title="Поделиться ссылкой"></th>
<th data-k="name">Имя</th><th data-k="comment">Комментарий</th><th class="r" data-k="size">Размер</th><th class="r" data-k="mtime">Изменён</th></tr></thead>
<tbody id="tb"></tbody></table>
<div id="st"></div>
<input type="file" id="fi" multiple style="display:none">
<input type="file" id="fd" webkitdirectory multiple style="display:none">
<div id="drop">Отпустите файлы для загрузки</div>
<div id="toast"><span>✅</span><span id="tm">Ссылка скопирована</span><span class="tl" id="tl"></span></div>
<div id="modal"><div class="w"><div class="h"><b id="mt"></b><button id="mc">✕ Закрыть</button></div><div class="b" id="mb"></div></div></div>
<div id="cmodal"><div class="w"><b id="cmt"></b><div id="cml"></div>
<div class="row"><button id="cmc">Отменить загрузку</button><button id="cms">Пропустить все</button><button id="cmr" class="danger">Заменить все</button></div></div></div>
<div id="lmodal"><form id="lf"><b>Вход для изменения файлов</b>
<input id="lu" placeholder="Логин" autocomplete="username" required>
<input id="lp" type="password" placeholder="Пароль" autocomplete="current-password" required>
<div id="le" class="err"></div>
<div class="row"><button type="button" id="lc">Отмена</button><button type="submit">Войти</button></div></form></div>
<script>
let admin = false, role = null, upDir = "";
const canWrite = () => admin || (role === "upload" && (cur === upDir || cur.startsWith(upDir + "/")));
let cur = decodeURIComponent(location.hash.slice(1) || ""), items = [], sel = new Set(), sortK = "name", sortD = 1;
const $ = id => document.getElementById(id);
const esc = s => s.replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const join = (a, b) => a ? a + "/" + b : b;
function fmt(n){ if(n < 1024) return n + " Б"; const u=["КБ","МБ","ГБ","ТБ"]; let i=-1; do{n/=1024;i++}while(n>=1024&&i<3); return n.toFixed(n<10?2:1)+" "+u[i]; }
function fdate(t){ if(!t) return ""; const d=new Date(t*1000), p=x=>String(x).padStart(2,"0"); return `${p(d.getDate())}.${p(d.getMonth()+1)}.${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`; }
const ARCH = /\.(zip|tar|tgz|tbz2?|txz|gz|bz2|xz|jar)$/i;
function getClip(){ try{ return JSON.parse(localStorage.shareClip||"null"); }catch(e){ return null; } }
function setClip(c){ if(c) localStorage.shareClip = JSON.stringify(c); else delete localStorage.shareClip; }
let inflight = 0;
async function api(url, body){
  inflight++;
  try{ return await api0(url, body); } finally{ inflight--; }
}
async function api0(url, body){
  const r = await fetch(url, body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  let j = {}; try{ j = await r.json(); }catch(e){}
  if(r.status === 401) setAdmin(false);
  if(!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}
function status(msg, err){ $("st").innerHTML = err ? '<span class="err">'+esc(msg)+'</span>' : esc(msg); }
async function load(){
  try{
    const j = await api("/api/list?path=" + encodeURIComponent(cur));
    cur = j.path; items = j.items; sel.clear();
    location.hash = encodeURIComponent(cur);
    status(`${items.length} объектов · свободно ${fmt(j.free)} из ${fmt(j.total)}`);
    render();
  }catch(e){ status(e.message, true); if(cur){ cur = ""; load(); } }
}
function render(){
  const parts = cur ? cur.split("/") : [];
  let h = '<a data-p="">share</a>', acc = "";
  parts.forEach(p => { acc = join(acc, p); h += ' / <a data-p="'+esc(acc)+'">'+esc(p)+'</a>'; });
  $("crumbs").innerHTML = h;
  document.body.classList.toggle("cw", canWrite());
  $("crumbs").querySelectorAll("a").forEach(a => a.onclick = () => { cur = a.dataset.p; load(); });
  const list = items.slice().sort((a,b) => (b.dir - a.dir) || (sortK === "name" || sortK === "comment" ? (a[sortK] || "").localeCompare(b[sortK] || "", "ru", {numeric:true}) : (a[sortK] - b[sortK])) * sortD);
  let rows = cur ? '<tr data-up="1"><td></td><td></td><td class="n"><span class="ic">⤴</span>..</td><td></td><td></td><td></td></tr>' : "";
  for(const it of list){
    const p = join(cur, it.name);
    rows += `<tr data-p="${esc(p)}" class="${sel.has(p)?"sel":""}"><td><input type="checkbox" ${sel.has(p)?"checked":""}></td>`+
      `<td class="sh"><button class="shb" title="Поделиться: скопировать ссылку">🔗</button></td>`+
      `<td class="n"><span class="ic">${it.dir?"📁":(ARCH.test(it.name)?"🗜":"📄")}</span>${esc(it.name)}</td>`+
      `<td class="cm" title="${admin?"Щелчок - изменить комментарий":""}">${esc(it.comment || "")}</td>`+
      `<td class="r">${fmt(it.size)}</td><td class="r">${fdate(it.mtime)}</td></tr>`;
  }
  $("tb").innerHTML = rows || '<tr><td></td><td colspan="5" style="color:#888">Папка пуста</td></tr>';
  $("tb").querySelectorAll("tr").forEach(tr => {
    if(tr.dataset.up){ tr.querySelector(".n").onclick = () => { cur = cur.split("/").slice(0,-1).join("/"); load(); }; return; }
    const p = tr.dataset.p; if(!p) return;
    const it = items.find(i => join(cur, i.name) === p);
    // выделение - только галочкой; щелчок по имени открывает папку / архив / скачивает файл
    tr.querySelector("input").onclick = e => { e.stopPropagation(); toggle(p, e.target.checked); };
    tr.querySelector(".n").onclick = () => open(it, p);
    tr.querySelector(".shb").onclick = e => { e.stopPropagation(); share(p); };
    tr.querySelector(".cm").onclick = e => { if(admin) editComment(e.currentTarget, it, p); };
  });
  $("all").checked = items.length > 0 && sel.size === items.length;
  updBtns();
}
// ---- комментарии (только admin) ----
function editComment(td, it, p){
  if(td.querySelector("input")) return;
  const inp = document.createElement("input"); inp.value = it.comment || ""; inp.maxLength = 1000;
  inp.placeholder = "Комментарий (Enter - сохранить, Esc - отмена)";
  td.textContent = ""; td.appendChild(inp); inp.focus();
  let done = false;
  const finish = async save => {
    if(done) return; done = true;
    const text = inp.value.trim();
    if(save && text !== (it.comment || "")){
      try{ const j = await api("/api/comment", {path:p, text}); it.comment = j.text; }
      catch(e){ alert(e.message); }
    }
    td.textContent = it.comment || "";
  };
  inp.onkeydown = e => { if(e.key === "Enter") finish(true); else if(e.key === "Escape") finish(false); };
  inp.onblur = () => finish(true);
}
// ---- поделиться ----
const shareUrl = p => location.origin + "/s/" + p.split("/").map(encodeURIComponent).join("/");
async function copyText(t){
  // navigator.clipboard работает только по https/localhost - на http://IP запасной путь через textarea
  if(navigator.clipboard && window.isSecureContext){ try{ await navigator.clipboard.writeText(t); return true; }catch(e){} }
  const ta = document.createElement("textarea"); ta.value = t; ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:-1000px;opacity:0"; document.body.appendChild(ta); ta.select();
  let ok = false; try{ ok = document.execCommand("copy"); }catch(e){} ta.remove(); return ok;
}
let toastT;
function toast(msg, sub){
  $("tm").textContent = msg; $("tl").textContent = sub || ""; $("toast").classList.add("show");
  clearTimeout(toastT); toastT = setTimeout(() => $("toast").classList.remove("show"), 2600);
}
async function share(p){
  const u = shareUrl(p);
  if(await copyText(u)) toast("Ссылка скопирована", u);
  else prompt("Скопируйте ссылку:", u);
}
function toggle(p, on){ on ? sel.add(p) : sel.delete(p); render(); }
function open(it, p){
  if(it.dir){ cur = p; load(); }
  else if(ARCH.test(it.name)) showArchive(p);
  else location.href = "/dl?path=" + encodeURIComponent(p);
}
function setAdmin(a, r){
  admin = !!a; role = admin ? "admin" : (r || null);
  document.body.classList.toggle("ro", !admin);
  document.body.classList.toggle("anon", !role);
  document.body.classList.toggle("cw", canWrite());
  $("bLogin").style.display = role ? "none" : "";
  $("who").textContent = admin ? "Вход выполнен: можно изменять файлы"
    : role === "upload" ? "Вход только для загрузки: в папку /" + upDir + ", без удаления и замены"
    : "Гость: только просмотр и скачивание";
}
async function checkMe(){ try{ const j = await api("/api/me"); if(j.upload_dir) upDir = j.upload_dir; setAdmin(j.admin, j.role); }catch(e){} }
function showLogin(){ $("le").textContent = ""; $("lp").value = ""; $("lmodal").style.display = "flex"; $("lu").focus(); }
$("bLogin").onclick = showLogin;
$("lc").onclick = () => $("lmodal").style.display = "none";
$("lf").onsubmit = async e => {
  e.preventDefault();
  try{ const j = await api("/api/login", {user:$("lu").value, pass:$("lp").value}); $("lmodal").style.display = "none"; setAdmin(j.admin, j.role); updBtns(); }
  catch(err){ $("le").textContent = err.message; }
};
$("bLogout").onclick = async () => { try{ await api("/api/logout", {}); }catch(e){} setAdmin(false); updBtns(); };
function updBtns(){
  const n = sel.size, one = n === 1 ? items.find(i => join(cur, i.name) === [...sel][0]) : null, c = getClip();
  $("bDl").disabled = n === 0; $("bRn").disabled = n !== 1; $("bCp").disabled = $("bCt").disabled = $("bRm").disabled = n === 0;
  $("bAr").disabled = !(one && !one.dir && ARCH.test(one.name));
  $("bPs").disabled = !c;
  $("clip").innerHTML = c ? `Буфер (${c.op === "move" ? "вырезано" : "скопировано"}): ${c.src.map(esc).join(", ")} <a href="#" id="clr">очистить</a>` : "";
  if(c) $("clr").onclick = e => { e.preventDefault(); setClip(null); updBtns(); };
}
$("all").onclick = e => { sel.clear(); if(e.target.checked) items.forEach(i => sel.add(join(cur, i.name))); render(); };
document.querySelectorAll("th[data-k]").forEach(th => th.onclick = () => { if(sortK === th.dataset.k) sortD = -sortD; else { sortK = th.dataset.k; sortD = 1; } render(); });
$("bRf").onclick = load;
$("bMk").onclick = async () => { const n = prompt("Имя новой папки:"); if(!n) return; try{ await api("/api/mkdir", {path:cur, name:n}); load(); }catch(e){ alert(e.message); } };
$("bRn").onclick = async () => { const p = [...sel][0], old = p.split("/").pop(); const n = prompt("Новое имя:", old); if(!n || n === old) return; try{ await api("/api/rename", {path:p, name:n}); load(); }catch(e){ alert(e.message); } };
$("bRm").onclick = async () => { if(!confirm("Удалить безвозвратно:\n" + [...sel].join("\n"))) return; try{ status("Удаление..."); await api("/api/delete", {paths:[...sel]}); load(); }catch(e){ alert(e.message); load(); } };
$("bCp").onclick = () => { setClip({op:"copy", src:[...sel]}); updBtns(); };
$("bCt").onclick = () => { setClip({op:"move", src:[...sel]}); updBtns(); };
$("bPs").onclick = async () => {
  const c = getClip(); if(!c) return;
  try{ status((c.op === "move" ? "Перемещение" : "Копирование") + "... (большие файлы - это может занять время)"); await api("/api/paste", {op:c.op, src:c.src, dest:cur}); if(c.op === "move") setClip(null); load(); }
  catch(e){ alert(e.message); load(); }
};
$("bDl").onclick = () => { [...sel].forEach((p, i) => setTimeout(() => { const a = document.createElement("a"); a.href = "/dl?path=" + encodeURIComponent(p); a.download = ""; document.body.appendChild(a); a.click(); a.remove(); }, i * 400)); };
$("bAr").onclick = () => showArchive([...sel][0]);
$("mc").onclick = () => $("modal").style.display = "none";
async function showArchive(p){
  $("mt").textContent = p; $("mb").innerHTML = '<p style="padding:14px">Чтение архива... (для больших сжатых архивов - до нескольких минут)</p>'; $("modal").style.display = "flex";
  try{
    const j = await api("/api/archive?path=" + encodeURIComponent(p));
    let total = 0, rows = "";
    for(const it of j.items){
      total += it.size;
      const link = it.dir ? esc(it.name) : `<a href="/api/archive_get?path=${encodeURIComponent(p)}&member=${encodeURIComponent(it.name)}">${esc(it.name)}</a>`;
      rows += `<tr><td>${it.dir?"📁":"📄"} ${link}</td><td class="r">${it.dir?"":fmt(it.size)}</td><td class="r">${fdate(it.mtime)}</td></tr>`;
    }
    $("mt").textContent = `${p} — ${j.kind}, ${j.items.length}${j.truncated?"+":""} объектов, ${fmt(total)} в распакованном виде`;
    $("mb").innerHTML = `<table><thead><tr><th>Имя (щелчок - скачать файл)</th><th class="r">Размер</th><th class="r">Дата</th></tr></thead><tbody>${rows}</tbody></table>`;
  }catch(e){ $("mb").innerHTML = '<p class="err" style="padding:14px">'+esc(e.message)+'</p>'; }
}
// ---- загрузка: одна общая шкала на всю очередь ----
let queue = [], busy = false, xhr = null, bt = null, upHide = null, lastLoad = 0;
function newBatch(){ return {n:0, done:0, ok:0, skip:0, total:0, sent:0, cur:"", curLoaded:0, errs:[]}; }
async function enqueue(files){
  if(!role){ showLogin(); return; } if(!canWrite()){ alert("Загружать можно только в папку /" + upDir); return; }
  const dir = cur, list = [...files].map(f => ({f, name: f.webkitRelativePath || f.relPath || f.name, dir, ow: false}));
  if(!list.length) return;
  // одним запросом узнаём, что уже есть, и спрашиваем один раз на всю пачку
  let ex = new Set();
  try{ ex = new Set((await api("/api/exists", {path: dir, names: list.map(q => q.name)})).exists); }catch(e){}
  let skipped = [], rejected = [];
  if(ex.size){
    const dup = list.filter(q => ex.has(q.name));
    if(admin){
      const a = await askConflict(dup.map(q => q.name));
      if(a === "cancel") return;
      if(a === "replace") dup.forEach(q => q.ow = true); else skipped = dup;
    } else rejected = dup;
  }
  if(!bt || (!busy && !queue.length)) bt = newBatch();
  clearTimeout(upHide);
  bt.n += list.length;
  for(const q of list){
    if(skipped.includes(q)){ bt.done++; bt.skip++; continue; }
    if(rejected.includes(q)){ bt.done++; bt.errs.push({name: q.name, msg: "уже существует, замена запрещена"}); continue; }
    queue.push(q); bt.total += q.f.size;
  }
  upDraw();
  if(!busy && !queue.length) upFinish(); else pump();
}
function askConflict(names){
  return new Promise(res => {
    const m = $("cmodal"), n = names.length;
    $("cmt").textContent = n === 1 ? "Файл уже существует:" : `Уже существуют файлов: ${n}`;
    $("cml").innerHTML = names.slice(0, 200).map(x => `<div>${esc(x)}</div>`).join("") + (n > 200 ? `<div>… и ещё ${n - 200}</div>` : "");
    $("cmr").textContent = n === 1 ? "Заменить" : "Заменить все";
    $("cms").textContent = n === 1 ? "Пропустить" : "Пропустить все";
    const done = a => { m.style.display = "none"; res(a); };
    $("cmr").onclick = () => done("replace"); $("cms").onclick = () => done("skip"); $("cmc").onclick = () => done("cancel");
    m.style.display = "flex";
  });
}
function upDraw(){
  const el = $("up"); if(!bt){ el.innerHTML = ""; return; }
  const fin = !busy && !queue.length, sent = bt.sent + bt.curLoaded;
  const pct = bt.total ? Math.min(100, Math.floor(100 * sent / bt.total)) : (fin ? 100 : 0);
  let head;
  if(fin){
    head = `<b>${bt.errs.length ? "Загрузка завершена с ошибками" : "Загрузка завершена"}</b>: ${bt.ok} из ${bt.n} файл(ов), ${fmt(bt.sent)}`
      + (bt.skip ? `, пропущено ${bt.skip}` : "") + ` <button class="upx" title="Закрыть">✕</button>`;
  } else {
    head = `<b>Загрузка ${pct}%</b> — файлов ${bt.done} из ${bt.n}, ${fmt(sent)} из ${fmt(bt.total)}`
      + ` <button class="upx" title="Отменить загрузку">Отменить</button><div class="upc">${esc(bt.cur)}</div>`;
  }
  let errs = "";
  if(bt.errs.length) errs = `<details class="err"><summary>Ошибок: ${bt.errs.length}</summary>${bt.errs.map(e => `<div>${esc(e.name)}: ${esc(e.msg)}</div>`).join("")}</details>`;
  el.innerHTML = `<div class="upi">${head}<div class="pb"><div style="width:${pct}%"></div></div>${errs}</div>`;
  el.querySelector(".upx").onclick = fin ? () => { bt = null; upDraw(); } : upCancel;
}
function upCancel(){
  if(!confirm("Отменить загрузку? Уже загруженные файлы останутся.")) return;
  bt.skip += queue.length; bt.done += queue.length; queue = [];
  if(xhr){ const x = xhr; xhr = null; x.abort(); }
}
function upRefresh(force){ const t = Date.now(); if(force || t - lastLoad > 2000){ lastLoad = t; load(); } }
function upNext(q, res, msg){
  // res: "ok" | "skip" | "err"
  bt.done++; bt.sent += q.f.size; bt.curLoaded = 0; xhr = null;
  if(res === "ok") bt.ok++; else if(res === "skip") bt.skip++; else bt.errs.push({name: q.name, msg});
  busy = false;
  if(queue.length){ if(q.dir === cur) upRefresh(false); pump(); return; }
  upFinish();
}
function upFinish(){
  upRefresh(true); upDraw();
  if(!bt.errs.length){ const b0 = bt; upHide = setTimeout(() => { if(bt === b0 && !busy && !queue.length){ bt = null; upDraw(); } }, 5000); }
}
function pump(){
  if(busy || !queue.length) return;
  busy = true;
  const q = queue.shift();
  bt.cur = q.name; bt.curLoaded = 0; upDraw();
  const send = ow => {
    const x = xhr = new XMLHttpRequest();
    x.open("PUT", `/api/upload?path=${encodeURIComponent(q.dir)}&name=${encodeURIComponent(q.name)}${ow?"&overwrite=1":""}`);
    x.upload.onprogress = e => { if(e.lengthComputable && xhr === x){ bt.curLoaded = q.f.size * e.loaded / e.total; upDraw(); } };
    x.onload = () => {
      if(x.status === 409 && !ow && admin && confirm(q.name + " появился на сервере во время загрузки. Заменить?")) return send(true);
      if(x.status >= 200 && x.status < 300) return upNext(q, "ok");
      if(x.status === 401) setAdmin(false);
      let m = "HTTP " + x.status; try{ m = JSON.parse(x.responseText).error; }catch(e){}
      upNext(q, "err", m);
    };
    x.onerror = () => upNext(q, "err", "ошибка сети");
    x.onabort = () => upNext(q, "skip");
    x.send(q.f);
  };
  send(q.ow);
}
$("bUp").onclick = () => $("fi").click();
$("bUpD").onclick = () => $("fd").click();
$("fi").onchange = e => { enqueue(e.target.files); e.target.value = ""; };
$("fd").onchange = e => { enqueue(e.target.files); e.target.value = ""; };
let dragN = 0;
addEventListener("dragenter", e => { e.preventDefault(); if(!canWrite()) return; dragN++; $("drop").style.display = "flex"; });
addEventListener("dragleave", e => { if(--dragN <= 0){ dragN = 0; $("drop").style.display = "none"; } });
addEventListener("dragover", e => e.preventDefault());
addEventListener("drop", async e => {
  e.preventDefault(); dragN = 0; $("drop").style.display = "none";
  const entries = [...e.dataTransfer.items].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  const files = [];
  const walk = (en, pre) => new Promise(res => {
    if(en.isFile) en.file(f => { f.relPath = pre + f.name; files.push(f); res(); }, () => res());
    else { const r = en.createReader(), all = []; const rd = () => r.readEntries(async es => { if(es.length){ all.push(...es); rd(); } else { for(const c of all) await walk(c, pre + en.name + "/"); res(); } }, () => res()); rd(); }
  });
  if(entries.length){ for(const en of entries) await walk(en, ""); enqueue(files); }
  else enqueue(e.dataTransfer.files);
});
addEventListener("keydown", e => {
  if($("cmodal").style.display === "flex"){ if(e.key === "Escape") $("cmc").click(); return; }
  if(e.target.tagName === "INPUT" || $("modal").style.display === "flex" || $("lmodal").style.display === "flex"){ if(e.key === "Escape"){ $("modal").style.display = "none"; $("lmodal").style.display = "none"; } return; }
  if(!admin && e.key !== "Backspace") return;
  if(e.key === "Delete" && sel.size) $("bRm").click();
  else if(e.key === "F2" && sel.size === 1) $("bRn").click();
  else if((e.ctrlKey||e.metaKey) && e.key.toLowerCase() === "c" && sel.size) $("bCp").click();
  else if((e.ctrlKey||e.metaKey) && e.key.toLowerCase() === "x" && sel.size) $("bCt").click();
  else if((e.ctrlKey||e.metaKey) && e.key.toLowerCase() === "v" && getClip()) $("bPs").click();
  else if((e.ctrlKey||e.metaKey) && e.key.toLowerCase() === "a"){ e.preventDefault(); $("all").checked = true; $("all").onclick({target:$("all")}); }
  else if(e.key === "Backspace" && cur){ cur = cur.split("/").slice(0,-1).join("/"); load(); }
});
addEventListener("storage", updBtns);
checkMe();
addEventListener("hashchange", () => { const h = decodeURIComponent(location.hash.slice(1)); if(h !== cur){ cur = h; load(); } });
// после обновления сервера открытая страница перезагружается сама - когда пользователь ничего не делает
const BUILD = "%%BUILD%%";
let newBuild = false;
const isOpen = id => $(id).style.display === "flex";
function idle(){
  return !busy && !queue.length && !inflight && !dragN &&
    !isOpen("modal") && !isOpen("cmodal") && !isOpen("lmodal") &&
    !document.querySelector("#tb input:not([type=checkbox])") &&
    (document.hidden || !sel.size);
}
function tryReload(){
  if(!newBuild || !idle()) return;
  try{ sessionStorage.swReload = JSON.stringify({sortK, sortD, y: scrollY}); }catch(e){}
  location.reload();
}
async function checkBuild(){
  if(newBuild) return tryReload();
  try{
    const j = await (await fetch("/api/version", {cache: "no-store"})).json();
    if(j.build && j.build !== BUILD){ newBuild = true; tryReload(); }
  }catch(e){}
}
setInterval(checkBuild, 60000);
setInterval(tryReload, 3000);
addEventListener("visibilitychange", () => { if(!document.hidden) checkBuild(); });
addEventListener("focus", checkBuild);
try{
  const r = sessionStorage.swReload && JSON.parse(sessionStorage.swReload);
  delete sessionStorage.swReload;
  if(r){ sortK = r.sortK || sortK; sortD = r.sortD || sortD; toast("Страница обновлена до новой версии");
         const y = r.y; if(y) setTimeout(() => scrollTo(0, y), 300); }
}catch(e){}
load();
</script></body></html>"""


SHARE_PAGE = r"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Файл · общая папка</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;
 font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#1f2937;
 background:radial-gradient(1200px 600px at 20% 0%,#3b5b8a 0%,transparent 60%),linear-gradient(160deg,#1e293b,#2c3e50 55%,#34495e)}
.card{background:#fff;width:min(440px,92vw);border-radius:20px;padding:36px 32px 28px;text-align:center;
 box-shadow:0 24px 60px rgba(0,0,0,.35);animation:up .35s ease-out}
@keyframes up{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
.ico{width:96px;height:96px;margin:0 auto 18px;border-radius:24px;display:flex;align-items:center;justify-content:center;
 font-size:52px;background:linear-gradient(145deg,#eff6ff,#dbeafe)}
.name{font-size:20px;font-weight:650;word-break:break-word;line-height:1.3;margin:0 0 8px}
.cmt{color:#374151;font-size:14.5px;line-height:1.45;margin:6px 0 10px;padding:8px 12px;background:#f9fafb;border-radius:10px;word-break:break-word}
.size{font-size:28px;font-weight:700;color:#2a6ebb;margin:14px 0 4px}
.meta{color:#6b7280;font-size:14px;line-height:1.6}
.dl{display:flex;align-items:center;justify-content:center;gap:10px;margin:24px 0 0;padding:14px 18px;width:100%;
 border:none;border-radius:12px;background:linear-gradient(135deg,#2a6ebb,#1d4f8f);color:#fff;font-size:17px;font-weight:600;
 text-decoration:none;cursor:pointer;box-shadow:0 8px 20px rgba(42,110,187,.35);transition:transform .12s,box-shadow .12s}
.dl:hover{transform:translateY(-1px);box-shadow:0 12px 26px rgba(42,110,187,.45)}
.dl:active{transform:none}
.sub{margin-top:10px;font-size:12.5px;color:#9ca3af}
.links{margin-top:18px;display:flex;gap:16px;justify-content:center;font-size:13.5px}
.links a{color:#2a6ebb;text-decoration:none;cursor:pointer}
.links a:hover{text-decoration:underline}
.foot{margin-top:22px;color:rgba(255,255,255,.55);font-size:12.5px}
.err .ico{background:linear-gradient(145deg,#fef2f2,#fee2e2)}
.sk{height:14px;border-radius:7px;background:linear-gradient(90deg,#eee,#f6f6f6,#eee);background-size:200% 100%;animation:sh 1.2s infinite;margin:10px auto}
@keyframes sh{to{background-position:-200% 0}}
#toast{position:fixed;left:50%;bottom:28px;transform:translate(-50%,30px);opacity:0;pointer-events:none;background:#111827;color:#fff;
 padding:11px 20px;border-radius:10px;font-size:14px;box-shadow:0 8px 24px rgba(0,0,0,.3);transition:opacity .25s,transform .25s}
#toast.show{opacity:1;transform:translate(-50%,0)}
</style></head><body>
<div class="card" id="card"><div class="ico">⏳</div><div class="sk" style="width:70%"></div><div class="sk" style="width:40%"></div></div>
<div class="foot">%%FOOT%%</div>
<div id="toast">✅ Ссылка скопирована</div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function fmt(n){ if(n < 1024) return n + " Б"; const u=["КБ","МБ","ГБ","ТБ"]; let i=-1; do{n/=1024;i++}while(n>=1024&&i<3); return n.toFixed(n<10?2:1)+" "+u[i]; }
function fdate(t){ const d=new Date(t*1000), p=x=>String(x).padStart(2,"0"); return `${p(d.getDate())}.${p(d.getMonth()+1)}.${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`; }
function plural(n, a, b, c){ const m = n % 10, h = n % 100; return n + " " + (m === 1 && h !== 11 ? a : m >= 2 && m <= 4 && (h < 12 || h > 14) ? b : c); }
function icon(it){
  if(it.dir) return "📁";
  const e = (it.name.split(".").pop() || "").toLowerCase();
  if(/^(iso|img|vhd|vhdx|vmdk|qcow2)$/.test(e)) return "💿";
  if(/^(zip|tar|tgz|gz|bz2|xz|7z|rar|zst|tbz2?|txz)$/.test(e)) return "🗜️";
  if(/^(png|jpe?g|gif|bmp|webp|svg)$/.test(e)) return "🖼️";
  if(/^(mp4|mkv|avi|mov|webm)$/.test(e)) return "🎬";
  if(/^(mp3|wav|flac|ogg)$/.test(e)) return "🎵";
  if(/^(pdf)$/.test(e)) return "📕";
  if(/^(docx?|odt|rtf)$/.test(e)) return "📘";
  if(/^(xlsx?|ods|csv)$/.test(e)) return "📗";
  if(/^(exe|msi|deb|rpm|appimage|sh|bat|ps1)$/.test(e)) return "⚙️";
  if(/^(txt|log|md|conf|ini|json|xml|ya?ml)$/.test(e)) return "📝";
  return "📄";
}
async function copyText(t){
  if(navigator.clipboard && window.isSecureContext){ try{ await navigator.clipboard.writeText(t); return true; }catch(e){} }
  const ta = document.createElement("textarea"); ta.value = t; ta.style.cssText = "position:fixed;top:-1000px;opacity:0";
  document.body.appendChild(ta); ta.select(); let ok = false; try{ ok = document.execCommand("copy"); }catch(e){} ta.remove(); return ok;
}
const path = location.pathname.replace(/^\/s\/?/, "").split("/").map(decodeURIComponent).join("/");
(async () => {
  try{
    const r = await fetch("/api/info?path=" + encodeURIComponent(path));
    const it = await r.json();
    if(!r.ok) throw new Error(it.error || ("HTTP " + r.status));
    document.title = it.name + " · общая папка";
    const dlName = it.dir ? it.name + ".zip" : it.name;
    const parent = it.path.split("/").slice(0, -1).join("/");
    $("card").innerHTML =
      `<div class="ico">${icon(it)}</div>`+
      `<div class="name">${esc(it.name)}</div>`+
      (it.comment ? `<div class="cmt">${esc(it.comment)}</div>` : "")+
      `<div class="meta">${it.dir ? "Папка · " + plural(it.files, "файл", "файла", "файлов") + (it.dirs ? ", " + plural(it.dirs, "папка", "папки", "папок") : "") : "Файл"}</div>`+
      `<div class="size">${fmt(it.size)}</div>`+
      `<div class="meta">Изменён ${fdate(it.mtime)}</div>`+
      `<a class="dl" href="/dl?path=${encodeURIComponent(it.path)}" download="${esc(dlName)}">⬇&nbsp; Скачать</a>`+
      (it.dir ? `<div class="sub">Папка скачается одним архивом .zip</div>` : "")+
      `<div class="links"><a id="cp">🔗 Копировать ссылку</a><a href="/#${encodeURIComponent(it.dir ? it.path : parent)}">📂 Открыть в общей папке</a></div>`;
    $("cp").onclick = async () => { if(await copyText(location.href)){ $("toast").classList.add("show"); setTimeout(() => $("toast").classList.remove("show"), 2200); } else prompt("Ссылка:", location.href); };
  }catch(e){
    $("card").classList.add("err");
    $("card").innerHTML = `<div class="ico">⚠️</div><div class="name">Не найдено</div><div class="meta">${esc(e.message)}</div>`+
      `<div class="links"><a href="/">Открыть общую папку</a></div>`;
  }
})();
// новая версия сервера - перезагрузить карточку
const BUILD = "%%BUILD%%";
async function checkBuild(){
  try{ const j = await (await fetch("/api/version", {cache: "no-store"})).json(); if(j.build && j.build !== BUILD) location.reload(); }catch(e){}
}
setInterval(checkBuild, 60000);
addEventListener("visibilitychange", () => { if(!document.hidden) checkBuild(); });
</script></body></html>"""


_e = html.escape
PAGE = PAGE.replace("%%TITLE%%", _e(TITLE)).replace("%%SMB%%", _e(" · ".join(SMB_PATHS)))
SHARE_PAGE = SHARE_PAGE.replace("%%FOOT%%", _e("Общая папка · " + (SMB_PATHS[0] if SMB_PATHS else TITLE)))
# сборка = хэш страниц: поменялись (новая версия) - открытые вкладки перезагрузятся сами (/api/version)
BUILD = hashlib.sha1((PAGE + SHARE_PAGE).encode()).hexdigest()[:12]
PAGE = PAGE.replace("%%BUILD%%", BUILD)
SHARE_PAGE = SHARE_PAGE.replace("%%BUILD%%", BUILD)


class Server(ThreadingHTTPServer):
    # в Windows SO_REUSEADDR позволяет второму процессу молча занять тот же порт
    allow_reuse_address = not WIN

    def handle_error(self, request, client_address):
        # обрыв соединения клиентом (закрыл вкладку, отменил скачивание) - не ошибка
        ex = sys.exc_info()[1]
        if isinstance(ex, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            log.debug("%s: соединение оборвано: %s", client_address[0], ex)
        else:
            log.exception("%s: ошибка при обработке запроса", client_address[0])


if __name__ == "__main__":
    os.umask(0o002)
    log.info("запуск: %s:%d, папка %s, пользователи: %s, уровень лога %s", BIND, PORT, ROOT,
             ", ".join("%s (%s)" % (u, r) for u, (_, r) in USERS.items()) or "нет",
             logging.getLevelName(logging.getLogger().level))
    if not USERS:
        log.warning("не задан ни один пользователь (SHARE_PASS / SHARE_UPLOAD_PASS) - только просмотр и скачивание")
    srv = Server((BIND, PORT), H)
    srv.daemon_threads = True
    srv.serve_forever()
