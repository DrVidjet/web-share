#!/usr/bin/env python3
"""web-share: веб-файловый менеджер общей папки.

Загрузка, скачивание (папки - zip), создание папок, переименование, удаление,
копирование / вырезание / вставка, просмотр содержимого архивов (zip, tar, tar.gz/bz2/xz, gz),
скачивание отдельного файла из архива, страница «поделиться». Только stdlib python3 (>= 3.7).
Настройки - .env рядом со скриптом (пишет start.sh, все ключи - в .env.example) или переменные окружения SHARE_*.
"""
import hmac
import ipaddress
import html
import secrets
import threading
import json
import os
import shutil
import stat
import tarfile
import time
import urllib.parse
import zipfile
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer



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
ROOT = os.path.realpath(C.get("SHARE_ROOT", "/srv/share"))
BIND = C.get("SHARE_BIND", "0.0.0.0")
PORT = int(C.get("SHARE_PORT", "8081"))
# с каких адресов можно открыть страницу (CIDR через запятую)
ALLOW = [ipaddress.ip_network(n.strip(), strict=False)
         for n in C.get("SHARE_ALLOW", "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8").split(",") if n.strip()]
TITLE = C.get("SHARE_TITLE") or os.path.basename(ROOT) or "share"
SMB_PATHS = [x.strip() for x in C.get("SHARE_SMB_PATHS", "").split(",") if x.strip()]  # только для показа в шапке
CHUNK = 1024 * 1024
# вход для изменения файлов (без входа - только просмотр и скачивание)
ADMIN_USER = C.get("SHARE_USER", "admin")
ADMIN_PASS = C.get("SHARE_PASS", "")
# upload: читать/скачивать всё, загружать новые файлы и создавать папки только внутри UPLOAD_DIR;
# без переименования, удаления, копирования/перемещения и перезаписи существующих файлов. Пустой логин - выключено.
UPLOAD_USER = C.get("SHARE_UPLOAD_USER", "")
UPLOAD_PASS = C.get("SHARE_UPLOAD_PASS", "")
UPLOAD_DIR = os.path.join(ROOT, C.get("SHARE_UPLOAD_DIR", "upload").strip("/"))
USERS = {}
if UPLOAD_USER and UPLOAD_PASS:
    USERS[UPLOAD_USER] = (UPLOAD_PASS, "upload")
if ADMIN_USER and ADMIN_PASS:
    USERS[ADMIN_USER] = (ADMIN_PASS, "admin")
SESSION_TTL = int(float(C.get("SHARE_SESSION_HOURS", "12")) * 3600)
SESSIONS = {}  # токен -> [время истечения, роль]
SLOCK = threading.Lock()
ARCH_LIMIT = 20000


class Err(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def resolve(rel, must_exist=True):
    rel = (rel or "").strip("/")
    p = os.path.realpath(os.path.join(ROOT, rel))
    if p != ROOT and not p.startswith(ROOT + os.sep):
        raise Err(403, "Путь вне общей папки")
    if must_exist and not os.path.lexists(p):
        raise Err(404, "Не найдено: /" + rel)
    return p


def relpath(p):
    r = os.path.relpath(p, ROOT)
    return "" if r == "." else r


def check_name(name):
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise Err(400, "Недопустимое имя: %r" % name)
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
        pass

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
            time.sleep(1)
            raise Err(403, "Неверный логин или пароль")
        t = secrets.token_urlsafe(32)
        now = time.time()
        with SLOCK:
            for k in [k for k, v in SESSIONS.items() if v[0] < now]:
                del SESSIONS[k]
            SESSIONS[t] = [now + SESSION_TTL, role]
        self.send_json({"ok": True, "admin": role == "admin", "role": role},
                       cookie="sw_sess=%s; Path=/; HttpOnly; SameSite=Strict" % t)

    def logout(self):
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
        else:
            code, msg = 500, "%s: %s" % (type(e).__name__, e)
        # тело запроса (загрузка) могло остаться непрочитанным - соединение не переиспользуем
        self.close_connection = True
        try:
            self.send_json({"error": msg}, code)
        except Exception:
            pass

    # ---------- routing ----------
    def do_GET(self):
        if not self.allowed():
            return self.send_error(403)
        route = urllib.parse.urlsplit(self.path).path
        try:
            if route == "/":
                b = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
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
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.handle_err(e)

    def do_PUT(self):
        if not self.allowed():
            return self.send_error(403)
        try:
            if urllib.parse.urlsplit(self.path).path == "/api/upload":
                self.upload()
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.handle_err(e)

    def do_POST(self):
        if not self.allowed():
            return self.send_error(403)
        route = urllib.parse.urlsplit(self.path).path
        try:
            d = self.body_json()
            if route == "/api/login":
                return self.login(d)
            if route == "/api/logout":
                return self.logout()
            if route in ("/api/rename", "/api/delete", "/api/paste"):
                self.need_admin()
            if route == "/api/mkdir":
                parent = resolve(d.get("path"))
                self.need_write(parent)
                p = os.path.join(parent, check_name(d.get("name", "").strip()))
                os.mkdir(p)
                self.send_json({"ok": True})
            elif route == "/api/rename":
                src = resolve(d.get("path"))
                if src == ROOT:
                    raise Err(400, "Нельзя переименовать корень")
                dst = os.path.join(os.path.dirname(src), check_name(d.get("name", "").strip()))
                if os.path.lexists(dst):
                    raise Err(409, "Уже существует: " + os.path.basename(dst))
                os.rename(src, dst)
                self.send_json({"ok": True})
            elif route == "/api/delete":
                for r in d.get("paths", []):
                    p = resolve(r)
                    if p == ROOT:
                        raise Err(400, "Нельзя удалить корень")
                    if os.path.isdir(p) and not os.path.islink(p):
                        shutil.rmtree(p)
                    else:
                        os.remove(p)
                self.send_json({"ok": True})
            elif route == "/api/paste":
                self.paste(d)
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.handle_err(e)

    # ---------- actions ----------
    def api_list(self):
        p = resolve(self.qget("path"))
        if not os.path.isdir(p):
            raise Err(400, "Не папка")
        items = []
        with os.scandir(p) as it:
            for de in it:
                try:
                    st = de.stat(follow_symlinks=True)
                    isdir = stat.S_ISDIR(st.st_mode)
                    items.append({"name": de.name, "dir": isdir,
                                  "size": dir_size(de.path) if isdir else st.st_size, "mtime": st.st_mtime})
                except OSError:
                    items.append({"name": de.name, "dir": False, "size": 0, "mtime": 0})
        du = shutil.disk_usage(p)
        self.send_json({"path": relpath(p), "items": items,
                        "free": du.free, "total": du.total})

    def api_info(self):
        """Карточка для страницы «поделиться»: имя, тип, размер, дата (для папки - ещё число файлов)."""
        p = resolve(self.qget("path"))
        st = os.stat(p)
        isdir = stat.S_ISDIR(st.st_mode)
        info = {"path": relpath(p), "name": os.path.basename(p) or "share", "dir": isdir,
                "mtime": st.st_mtime, "size": st.st_size}
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
            self.send_zip(p)
            return
        size = os.path.getsize(p)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        code = 200
        if rng and rng.startswith("bytes=") and size > 0:
            a, _, b = rng[6:].split(",")[0].partition("-")
            if a:
                start = int(a)
                end = int(b) if b else size - 1
            else:
                start = max(0, size - int(b))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        length = end - start + 1 if size else 0
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", self.disp(os.path.basename(p)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                b = f.read(min(CHUNK, left))
                if not b:
                    break
                self.wfile.write(b)
                left -= len(b)

    def send_zip(self, p):
        """Папка -> zip потоком (без сжатия: ISO/архивы всё равно не жмутся, а так быстро; ZIP64 для >4 ГБ)."""
        base = os.path.basename(p) or "share"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", self.disp(base + ".zip"))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for d, dirs, files in os.walk(p):
                dirs.sort()
                arc = os.path.join(base, os.path.relpath(d, p)) if d != p else base
                if not files and not dirs:
                    z.writestr(zipfile.ZipInfo(arc + "/"), b"")
                for f in sorted(files):
                    fp = os.path.join(d, f)
                    if not os.path.isfile(fp):
                        continue  # битые ссылки, сокеты и т.п.
                    try:
                        zi = zipfile.ZipInfo.from_file(fp, os.path.join(arc, f))
                        zi.compress_type = zipfile.ZIP_STORED
                        with open(fp, "rb") as src, z.open(zi, "w", force_zip64=zi.file_size > 0x7FFFFFFF) as dst:
                            shutil.copyfileobj(src, dst, CHUNK)
                    except OSError:
                        pass

    def archive_get(self):
        p = resolve(self.qget("path"))
        member = self.qget("member")
        kind = archive_kind(p)
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

    def upload(self):
        d = resolve(self.qget("path"))
        if not os.path.isdir(d):
            raise Err(400, "Не папка")
        rel_name = self.qget("name").strip("/")
        parts = [check_name(x) for x in rel_name.split("/") if x]
        if not parts:
            raise Err(400, "Нет имени файла")
        dst = os.path.realpath(os.path.join(d, *parts))
        if not dst.startswith(ROOT + os.sep):
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
            if role == "admin":
                os.replace(tmp, dst)
            else:
                os.link(tmp, dst)  # не заменит файл, появившийся за время загрузки (FileExistsError)
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
            elif op == "copy":
                dst = unique_dest(dest, os.path.basename(src))
                if os.path.isdir(src) and not os.path.islink(src):
                    shutil.copytree(src, dst, symlinks=True)
                else:
                    shutil.copy2(src, dst, follow_symlinks=False)
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
.upi{font-size:13px;margin:3px 0}
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
#lmodal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:11;align-items:center;justify-content:center}
#lmodal form{background:#fff;padding:18px 20px;border-radius:6px;width:300px;display:flex;flex-direction:column;gap:9px}
#lmodal input{padding:7px;font-size:14px;border:1px solid #b9c2cc;border-radius:4px}
#lmodal .row{display:flex;gap:8px;justify-content:flex-end}
.err{color:#c9302c}
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
<th data-k="name">Имя</th><th class="r" data-k="size">Размер</th><th class="r" data-k="mtime">Изменён</th></tr></thead>
<tbody id="tb"></tbody></table>
<div id="st"></div>
<input type="file" id="fi" multiple style="display:none">
<input type="file" id="fd" webkitdirectory multiple style="display:none">
<div id="drop">Отпустите файлы для загрузки</div>
<div id="toast"><span>✅</span><span id="tm">Ссылка скопирована</span><span class="tl" id="tl"></span></div>
<div id="modal"><div class="w"><div class="h"><b id="mt"></b><button id="mc">✕ Закрыть</button></div><div class="b" id="mb"></div></div></div>
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
async function api(url, body){
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
  const list = items.slice().sort((a,b) => (b.dir - a.dir) || (sortK === "name" ? a.name.localeCompare(b.name, "ru", {numeric:true}) : (a[sortK] - b[sortK])) * sortD);
  let rows = cur ? '<tr data-up="1"><td></td><td></td><td class="n"><span class="ic">⤴</span>..</td><td></td><td></td></tr>' : "";
  for(const it of list){
    const p = join(cur, it.name);
    rows += `<tr data-p="${esc(p)}" class="${sel.has(p)?"sel":""}"><td><input type="checkbox" ${sel.has(p)?"checked":""}></td>`+
      `<td class="sh"><button class="shb" title="Поделиться: скопировать ссылку">🔗</button></td>`+
      `<td class="n"><span class="ic">${it.dir?"📁":(ARCH.test(it.name)?"🗜":"📄")}</span>${esc(it.name)}</td>`+
      `<td class="r">${fmt(it.size)}</td><td class="r">${fdate(it.mtime)}</td></tr>`;
  }
  $("tb").innerHTML = rows || '<tr><td></td><td colspan="4" style="color:#888">Папка пуста</td></tr>';
  $("tb").querySelectorAll("tr").forEach(tr => {
    if(tr.dataset.up){ tr.querySelector(".n").onclick = () => { cur = cur.split("/").slice(0,-1).join("/"); load(); }; return; }
    const p = tr.dataset.p; if(!p) return;
    const it = items.find(i => join(cur, i.name) === p);
    // выделение - только галочкой; щелчок по имени открывает папку / архив / скачивает файл
    tr.querySelector("input").onclick = e => { e.stopPropagation(); toggle(p, e.target.checked); };
    tr.querySelector(".n").onclick = () => open(it, p);
    tr.querySelector(".shb").onclick = e => { e.stopPropagation(); share(p); };
  });
  $("all").checked = items.length > 0 && sel.size === items.length;
  updBtns();
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
// ---- загрузка ----
let queue = [], busy = false;
function enqueue(files){ if(!role){ showLogin(); return; } if(!canWrite()){ alert("Загружать можно только в папку /" + upDir); return; } for(const f of files) queue.push({f, name: f.webkitRelativePath || f.relPath || f.name, dir: cur}); pump(); }
function pump(){
  if(busy || !queue.length) return;
  busy = true;
  const q = queue.shift(), div = document.createElement("div");
  div.className = "upi"; div.innerHTML = `${esc(q.name)} (${fmt(q.f.size)}) <span></span><div class="pb"><div></div></div>`;
  $("up").appendChild(div);
  const send = ow => {
    const x = new XMLHttpRequest();
    x.open("PUT", `/api/upload?path=${encodeURIComponent(q.dir)}&name=${encodeURIComponent(q.name)}${ow?"&overwrite=1":""}`);
    x.upload.onprogress = e => { if(e.lengthComputable){ div.querySelector(".pb div").style.width = (100*e.loaded/e.total) + "%"; div.querySelector("span").textContent = Math.floor(100*e.loaded/e.total) + "%"; } };
    x.onload = () => {
      if(x.status === 409 && !ow && admin && confirm(q.name + " уже существует. Заменить?")) return send(true);
      if(x.status >= 200 && x.status < 300){ div.querySelector("span").textContent = "готово"; setTimeout(() => div.remove(), 3000); }
      else { if(x.status === 401) setAdmin(false); let m = "HTTP " + x.status; try{ m = JSON.parse(x.responseText).error; }catch(e){} div.querySelector("span").innerHTML = '<span class="err">'+esc(m)+'</span>'; }
      busy = false; if(q.dir === cur) load(); pump();
    };
    x.onerror = () => { div.querySelector("span").innerHTML = '<span class="err">ошибка сети</span>'; busy = false; pump(); };
    x.send(q.f);
  };
  // файл в текущей папке уже есть - спрашиваем до отправки, а не после
  if(q.dir === cur && !q.name.includes("/") && items.some(i => i.name === q.name)){
    if(!admin){ div.querySelector("span").innerHTML = '<span class="err">уже существует, замена запрещена</span>'; busy = false; pump(); }
    else if(confirm(q.name + " уже существует. Заменить?")) send(true);
    else { div.remove(); busy = false; pump(); }
  } else send(false);
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
</script></body></html>"""


_e = html.escape
PAGE = PAGE.replace("%%TITLE%%", _e(TITLE)).replace("%%SMB%%", _e(" · ".join(SMB_PATHS)))
SHARE_PAGE = SHARE_PAGE.replace("%%FOOT%%", _e("Общая папка · " + (SMB_PATHS[0] if SMB_PATHS else TITLE)))


if __name__ == "__main__":
    os.umask(0o002)
    if not USERS:
        print("web-share: не задан ни один пользователь (SHARE_PASS / SHARE_UPLOAD_PASS) - только просмотр и скачивание",
              flush=True)
    srv = ThreadingHTTPServer((BIND, PORT), H)
    srv.daemon_threads = True
    srv.serve_forever()
