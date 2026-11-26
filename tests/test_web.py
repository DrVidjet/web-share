#!/usr/bin/env python3
"""Тесты web-share через HTTP: настоящий share-web.py во временной папке, только stdlib.
Работают в Linux и в Windows (их гоняет CI на обеих системах).

    python3 tests/test_web.py           # все тесты
    python3 tests/test_web.py -v -k zip # подробно, только с «zip» в имени
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "share-web.py")
WIN = os.name == "nt"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    """share-web.py в отдельном процессе; настройки - только из переменных окружения."""

    def __init__(self, tmp, **conf):
        self.tmp = tmp
        self.port = conf.pop("port", None) or free_port()
        self.share = os.path.join(tmp, "share")
        os.makedirs(self.share, exist_ok=True)
        state = os.path.join(tmp, "state")
        os.makedirs(state, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("SHARE_")}
        env.update(SHARE_CONF=os.path.join(tmp, "нет.env"), STATE_DIRECTORY=state, SHARE_ROOT=self.share,
                   SHARE_BIND="127.0.0.1", SHARE_PORT=str(self.port), SHARE_USER="admin", SHARE_PASS="adminpw",
                   SHARE_UPLOAD_USER="up", SHARE_UPLOAD_PASS="uppw", SHARE_UPLOAD_DIR="upload",
                   SHARE_LOG_LEVEL="DEBUG", SHARE_LOG_FILE=os.path.join(tmp, "log", "web-share.log"))
        env.update({k: str(v) for k, v in conf.items()})
        self.log = os.path.join(tmp, "log", "web-share.log")
        self.proc = subprocess.Popen([sys.executable, "-I", APP], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def wait(self, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                raise RuntimeError("сервер завершился: " + self.proc.stdout.read().decode(errors="replace"))
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("сервер не открыл порт %d" % self.port)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc.stdout.close()


class Client:
    def __init__(self, port):
        self.base = "http://127.0.0.1:%d" % port
        self.cookie = ""

    def req(self, method, path, data=None, headers=None, raw=False):
        h = dict(headers or {})
        if self.cookie:
            h["Cookie"] = self.cookie
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                code, body, hdrs = resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            code, body, hdrs = e.code, e.read(), e.headers
        c = hdrs.get("Set-Cookie")
        if c:
            self.cookie = c.split(";", 1)[0]
        if raw:
            return code, body, hdrs
        try:
            return code, json.loads(body.decode() or "null")
        except ValueError:
            return code, body.decode(errors="replace")

    def get(self, route, **q):
        return self.req("GET", route + ("?" + urllib.parse.urlencode(q) if q else ""))

    def post(self, path, obj):
        return self.req("POST", path, obj)

    def login(self, user, pw):
        return self.post("/api/login", {"user": user, "pass": pw})

    def upload(self, path, name, data, overwrite=False):
        q = {"path": path, "name": name}
        if overwrite:
            q["overwrite"] = "1"
        return self.req("PUT", "/api/upload?" + urllib.parse.urlencode(q), data)

    def names(self, path=""):
        code, d = self.get("/api/list", path=path)
        assert code == 200, (code, d)
        return sorted(i["name"] for i in d["items"])


class WebShareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="web-share-test-")
        share = os.path.join(cls.tmp, "share")
        os.makedirs(os.path.join(share, "docs", "sub"))
        os.makedirs(os.path.join(share, "lost+found"))
        os.makedirs(os.path.join(share, "upload"))
        with open(os.path.join(share, "lost+found", "secret.txt"), "w", encoding="utf-8") as f:
            f.write("секрет")
        with open(os.path.join(share, "docs", "a.txt"), "w", encoding="utf-8") as f:
            f.write("привет")
        with open(os.path.join(share, "docs", "sub", "b.bin"), "wb") as f:
            f.write(os.urandom(300000))
        with open(os.path.join(share, "big.bin"), "wb") as f:
            f.write(bytes(range(256)) * 4096)
        with zipfile.ZipFile(os.path.join(share, "arch.zip"), "w") as z:
            z.writestr("in/zip.txt", "из zip")
        with tarfile.open(os.path.join(share, "arch.tar.gz"), "w:gz") as t:
            b = "из tar".encode()
            ti = tarfile.TarInfo("in/tar.txt")
            ti.size = len(b)
            t.addfile(ti, io.BytesIO(b))
        cls.srv = Server(cls.tmp).wait()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def client(self, who=None):
        c = Client(self.srv.port)
        if who == "admin":
            self.assertEqual(c.login("admin", "adminpw")[0], 200)
        elif who == "up":
            self.assertEqual(c.login("up", "uppw")[0], 200)
        return c

    def path(self, *p):
        return os.path.join(self.srv.share, *p)

    # ---------- просмотр ----------
    def test_page_and_version(self):
        c = self.client()
        code, body, _ = c.req("GET", "/", raw=True)
        self.assertEqual(code, 200)
        self.assertIn(b"<html", body.lower())
        code, d = c.get("/api/version")
        self.assertEqual(code, 200)
        self.assertRegex(d["build"], "^[0-9a-f]{12}$")
        self.assertEqual(c.req("GET", "/s/docs/a.txt", raw=True)[0], 200)

    def test_list_and_info(self):
        c = self.client()
        self.assertEqual(c.names(), ["arch.tar.gz", "arch.zip", "big.bin", "docs", "upload"])
        self.assertEqual(c.names("docs"), ["a.txt", "sub"])
        code, d = c.get("/api/info", path="docs")
        self.assertEqual(code, 200)
        self.assertEqual((d["dir"], d["files"], d["dirs"], d["path"]), (True, 2, 1, "docs"))
        code, d = c.get("/api/list", path="docs/sub")
        self.assertEqual(d["path"], "docs/sub")  # в Windows тоже через "/"

    def test_hidden(self):
        c = self.client()
        self.assertNotIn("lost+found", c.names())
        for p in ("lost+found", "lost+found/secret.txt", "/lost+found/"):
            self.assertEqual(c.get("/api/list", path=p)[0], 404, p)
            self.assertEqual(c.get("/dl", path=p)[0], 404, p)
        if WIN:
            for p in ("LOST+FOUND", "lost+found.", "lost+found\\secret.txt"):
                self.assertEqual(c.get("/dl", path=p)[0], 404, p)

    def test_outside_root(self):
        c = self.client()
        for p in ("../", "docs/../../", "/../../etc/passwd"):
            self.assertIn(c.get("/dl", path=p)[0], (403, 404), p)
        if WIN:
            for p in ("..\\", "docs\\..\\..\\", "C:\\Windows\\win.ini", "docs/a.txt::$DATA"):
                self.assertIn(c.get("/dl", path=p)[0], (400, 403, 404), p)

    # ---------- скачивание ----------
    def test_download_and_range(self):
        c = self.client()
        full = open(self.path("big.bin"), "rb").read()
        code, body, h = c.req("GET", "/dl?path=big.bin", raw=True)
        self.assertEqual((code, body), (200, full))
        self.assertIn("big.bin", h["Content-Disposition"])
        code, body, h = c.req("GET", "/dl?path=big.bin", headers={"Range": "bytes=1000-1999"}, raw=True)
        self.assertEqual((code, body), (206, full[1000:2000]))
        self.assertEqual(h["Content-Range"], "bytes 1000-1999/%d" % len(full))
        code, _, _ = c.req("GET", "/dl?path=big.bin", headers={"Range": "bytes=%d-" % (len(full) + 5)}, raw=True)
        self.assertEqual(code, 416)

    def test_zip_folder(self):
        c = self.client()
        code, body, h = c.req("GET", "/dl?path=docs", raw=True)
        self.assertEqual(code, 200)
        self.assertEqual(int(h["Content-Length"]), len(body))
        z = zipfile.ZipFile(io.BytesIO(body))
        self.assertIsNone(z.testzip())
        self.assertEqual(z.read("docs/a.txt").decode(), "привет")
        self.assertEqual(z.read("docs/sub/b.bin"), open(self.path("docs", "sub", "b.bin"), "rb").read())
        # докачка: кусок с середины совпадает с тем же местом целого архива
        etag = h["ETag"]
        code, part, _ = c.req("GET", "/dl?path=docs", headers={"Range": "bytes=100-", "If-Range": etag}, raw=True)
        self.assertEqual((code, part), (206, body[100:]))

    def test_zip_root_skips_hidden(self):
        code, body, _ = self.client().req("GET", "/dl?path=", raw=True)
        self.assertEqual(code, 200)
        names = zipfile.ZipFile(io.BytesIO(body)).namelist()
        self.assertTrue(any(n.endswith("/docs/a.txt") for n in names), names)
        self.assertFalse([n for n in names if "lost+found" in n], names)

    def test_archives(self):
        c = self.client()
        for arch, member, text in (("arch.zip", "in/zip.txt", "из zip"), ("arch.tar.gz", "in/tar.txt", "из tar")):
            code, d = c.get("/api/archive", path=arch)
            self.assertEqual(code, 200, d)
            self.assertIn(member, [i["name"] for i in d["items"]])
            code, body, _ = c.req("GET", "/api/archive_get?" + urllib.parse.urlencode(
                {"path": arch, "member": member}), raw=True)
            self.assertEqual((code, body.decode()), (200, text))

    # ---------- права ----------
    def test_login(self):
        c = self.client()
        self.assertEqual(c.login("admin", "неверно")[0], 403)
        self.assertEqual(c.get("/api/me")[1]["role"], None)
        self.assertEqual(c.login("admin", "adminpw")[0], 200)
        self.assertEqual(c.get("/api/me")[1]["role"], "admin")
        self.assertEqual(c.post("/api/logout", {})[0], 200)
        self.assertEqual(c.get("/api/me")[1]["role"], None)

    def test_guest_cannot_change(self):
        c = self.client()
        self.assertEqual(c.upload("", "g.txt", b"x")[0], 401)
        self.assertEqual(c.post("/api/mkdir", {"path": "", "name": "g"})[0], 401)
        self.assertEqual(c.post("/api/delete", {"paths": ["docs/a.txt"]})[0], 401)
        self.assertTrue(os.path.exists(self.path("docs", "a.txt")))

    def test_upload_user(self):
        c = self.client("up")
        self.assertEqual(c.upload("", "root.txt", b"x")[0], 403)
        self.assertEqual(c.upload("upload", "sub/new.txt", b"new")[0], 200)
        self.assertEqual(open(self.path("upload", "sub", "new.txt"), "rb").read(), b"new")
        self.assertEqual(c.upload("upload", "sub/new.txt", b"again", overwrite=True)[0], 409)
        self.assertEqual(open(self.path("upload", "sub", "new.txt"), "rb").read(), b"new")
        self.assertEqual(c.post("/api/mkdir", {"path": "upload", "name": "dir"})[0], 200)
        self.assertEqual(c.post("/api/rename", {"path": "upload/dir", "name": "x"})[0], 403)
        self.assertEqual(c.post("/api/delete", {"paths": ["upload/sub/new.txt"]})[0], 403)

    # ---------- изменения ----------
    def test_admin_file_ops(self):
        c = self.client("admin")
        self.assertEqual(c.post("/api/mkdir", {"path": "", "name": "work"})[0], 200)
        self.assertEqual(c.upload("work", "deep/f.txt", "файл".encode())[0], 200)
        self.assertEqual(c.upload("work", "deep/f.txt", b"2")[0], 409)
        self.assertEqual(c.upload("work", "deep/f.txt", b"2", overwrite=True)[0], 200)
        self.assertEqual(open(self.path("work", "deep", "f.txt"), "rb").read(), b"2")
        self.assertEqual(c.post("/api/exists", {"path": "work", "names": ["deep/f.txt", "nope"]})[1],
                         {"exists": ["deep/f.txt"]})
        # комментарий едет вместе с файлом
        self.assertEqual(c.post("/api/comment", {"path": "work/deep/f.txt", "text": "  мой   коммент "})[1]["text"],
                         "мой коммент")
        self.assertEqual(c.post("/api/rename", {"path": "work/deep", "name": "renamed"})[0], 200)
        items = {i["name"]: i for i in c.get("/api/list", path="work/renamed")[1]["items"]}
        self.assertEqual(items["f.txt"]["comment"], "мой коммент")
        self.assertEqual(c.post("/api/paste", {"op": "copy", "src": ["work/renamed/f.txt"], "dest": "work"})[0], 200)
        self.assertEqual(c.post("/api/paste", {"op": "copy", "src": ["work/renamed/f.txt"], "dest": "work"})[0], 200)
        self.assertEqual(c.names("work"), ["f (копия).txt", "f.txt", "renamed"])
        self.assertEqual(c.post("/api/paste", {"op": "move", "src": ["work/f.txt"], "dest": "work/renamed"})[0], 409)
        self.assertEqual(c.post("/api/paste", {"op": "move", "src": ["work/renamed"], "dest": "work/renamed"})[0], 400)
        self.assertEqual(c.post("/api/delete", {"paths": ["work"]})[0], 200)
        self.assertFalse(os.path.exists(self.path("work")))
        self.assertEqual(c.post("/api/delete", {"paths": [""]})[0], 400)

    def test_bad_names(self):
        c = self.client("admin")
        for n in ("", ".", "..", "a/b"):
            self.assertEqual(c.post("/api/mkdir", {"path": "", "name": n})[0], 400, n)
        if WIN:
            for n in ("con", "NUL.txt", "a:b", "a*b", "trail.", "trail "):
                self.assertEqual(c.post("/api/mkdir", {"path": "", "name": n})[0], 400, n)
                self.assertEqual(c.upload("", n, b"x")[0], 400, n)

    def test_log_file(self):
        with open(self.srv.log, encoding="utf-8") as f:
            self.assertIn("запуск:", f.read())


class ServerTest(unittest.TestCase):
    """Отдельные запуски сервера с другими настройками."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="web-share-test-")
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, sub, **conf):
        s = Server(os.path.join(self.tmp, sub), **conf)
        self.servers.append(s)
        return s

    def test_allow(self):
        s = self.start("a", SHARE_ALLOW="10.0.0.0/8").wait()
        self.assertEqual(Client(s.port).get("/api/list")[0], 403)

    def test_port_busy(self):
        # второй сервер на занятом порту должен упасть, а не тихо делить порт (SO_REUSEADDR в Windows)
        a = self.start("a").wait()
        b = self.start("b", port=a.port)
        try:
            b.proc.wait(15)
        except subprocess.TimeoutExpired:
            self.fail("второй сервер запустился на занятом порту")
        self.assertNotEqual(b.proc.returncode, 0)
        self.assertEqual(Client(a.port).get("/api/version")[0], 200)


if __name__ == "__main__":
    unittest.main()
