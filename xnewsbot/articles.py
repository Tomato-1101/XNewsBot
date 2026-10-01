"""ニュース候補の元記事を取りに行き、本文の先頭を `body` に入れる(要約を見出しの言い換えで終わらせないため)。

キュレーションのヘッドレス Claude は権限を Read/Write に絞っていて Web を読めない(プロンプトインジェクション対策)。
そこで収集側(このプログラム)が本文を先に取り、raw JSON に載せて渡す。

- 対象: source=="news" で、URL が Google ニュースの転送 URL でも有料媒体でもない候補だけ。
  Google ニュースの転送 URL は解決しない(大量に解決すると 429・規約リスク)。有料媒体は本文が取れず時間の無駄。
- 並列8・1件おおむね15秒・全体 budget_s 秒で打ち切る。失敗(タイムアウト・HTML 以外・抽出不能)は無視して空のまま。
- 接続先は公開インターネットだけ(外部 RSS の URL やリダイレクトで内部ネットワークへ届かないようにする)。
- 抽出は trafilatura(本文以外のナビ・広告を落とす)。
"""

from __future__ import annotations

import http.client
import ipaddress
import queue
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BODY_MAX_CHARS = 600
_WORKERS = 8
_ITEM_TIMEOUT = 8       # ソケット操作1回あたり(少しずつ返すサーバだと総時間は縛れない)
_ITEM_TOTAL_S = 15      # 1件あたりの総時間。本文の読み込みをこれで打ち切る
_CHUNK = 65536
_MAX_BYTES = 3_000_000  # 巨大ページで抽出が詰まらないよう読み込みを打ち切る
_MAX_REDIRECTS = 5
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# 本文が会員限定で取れない媒体(サブドメインも含む)。取得しても見出しと冒頭しか無い。
PAYWALL_DOMAINS = (
    "nikkei.com", "bloomberg.com", "bloomberg.co.jp", "wsj.com", "ft.com", "economist.com",
    "nytimes.com", "washingtonpost.com", "barrons.com", "theinformation.com",
)
# openrouter.ai・huggingface.co は JS 描画で本文が取れない。reddit.com(www/old 含む)は自動取得を拒否される
_SKIP_HOSTS = ("news.google.com", "openrouter.ai", "huggingface.co", "reddit.com")
_SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".mp4", ".zip")


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _match(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_target(cand: dict) -> bool:
    """本文取得の対象か(ニュース候補・直リンク・有料媒体以外・HTML らしい URL)。"""
    if cand.get("source") != "news":
        return False
    url = cand.get("url") or ""
    if not url.startswith(("http://", "https://")):
        return False
    host = _host(url)
    if not host or _match(host, _SKIP_HOSTS) or _match(host, PAYWALL_DOMAINS):
        return False
    path = urllib.parse.urlsplit(url).path.lower()
    return not path.endswith(_SKIP_EXT)


def _public_addrs(host: str) -> list[str]:
    """host を名前解決し、解決先が全部公開アドレスならその IP 一覧(解決順・重複なし)を返す。
    1つでも loopback/private/link-local 等なら空。

    記事 URL は外部 RSS 由来で信用できないので、localhost やクラウドのメタデータ
    (169.254.169.254)・社内 LAN へ取りに行かないよう止める(SSRF 対策)。
    IP リテラル(10 進表記なども)も getaddrinfo が実アドレスに直すので同じ判定に乗る。
    """
    if not host:
        return []
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError, ValueError):
        return []
    # IPv6 のスコープ ID(%en0)は外す。接続はこの順に試すので set でなく順序付きで重複を落とす
    addrs = list(dict.fromkeys(info[4][0].split("%", 1)[0] for info in infos))
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            return []
        if not ip.is_global or ip.is_multicast:
            return []
    return addrs


def _is_public_host(host: str) -> bool:
    """host の解決先が全部公開アドレスか(事前の早期判定用。本体の検査は接続時の _connect_public)。

    ここで通っても、接続時に名前解決をやり直して DNS が内部 IP へ切り替わっていれば(DNS rebinding)
    接続時の検査で止まる。
    """
    return bool(_public_addrs(host))


def _connect_public(host: str, port: int, timeout, source_address=None) -> socket.socket:
    """host を1回だけ名前解決し、全部が公開アドレスなら、その検査済み IP へ直接 TCP 接続する。

    検査と接続で別々に名前解決すると、その間に DNS が内部 IP へ切り替わったとき(DNS rebinding)
    検査を通ったまま内部へ繋がる。検査した IP そのものに繋ぐことでこの隙間を塞ぐ。
    """
    addrs = _public_addrs(host)
    if not addrs:
        raise OSError(f"公開アドレス以外への接続を拒否: {host[:200]}")
    err: OSError | None = None
    for ip in addrs:
        try:
            return socket.create_connection((ip, port), timeout, source_address)
        except OSError as e:
            err = e
    raise err


class _PublicHTTPConnection(http.client.HTTPConnection):
    """接続のたびに _connect_public を通す(初回もリダイレクト後も同じ検査がかかる)。"""

    def connect(self):
        self.sock = _connect_public(self.host, self.port, self.timeout, self.source_address)


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    """TCP は検査済み IP へ繋ぎ、TLS の SNI と証明書のホスト名検証には元のホスト名を使う。"""

    def connect(self):
        self.sock = _connect_public(self.host, self.port, self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PublicHTTPConnection, req)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=self._context)


class _PublicOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """リダイレクト先を http/https に限り、回数を絞る。公開アドレスかは早期判定し、
    本体の検査はリダイレクト後の接続でも _connect_public が行う(公開サイトから内部へ飛ばされるのを防ぐ)。"""

    max_redirections = _MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith(("http://", "https://")) or not _is_public_host(_host(newurl)):
            raise urllib.error.URLError(f"公開アドレス以外へのリダイレクトを拒否: {newurl[:200]}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# ProxyHandler({}): 環境変数のプロキシ設定で接続先(=検査対象)が変わらないよう、プロキシを使わない。
# HTTPS の証明書検証は ssl.create_default_context() の既定(ホスト名検証あり・CERT_REQUIRED)のまま。
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _PublicHTTPHandler,
    _PublicHTTPSHandler(context=ssl.create_default_context()),
    _PublicOnlyRedirect,
)


def _download(url: str) -> bytes | None:
    # 事前の早期判定(明らかな内部宛てで要求を組み立てない)。本体の検査は接続時の _connect_public
    if not _is_public_host(_host(url)):
        return None
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ja,en;q=0.8",
    })
    started = time.monotonic()
    try:
        with _OPENER.open(req, timeout=_ITEM_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype and "html" not in ctype:
                return None  # PDF・画像などは本文抽出の対象外
            # read(n) は n バイト揃うまで待つので、少しずつ返すサーバだと1件が無制限に延びる。
            # read1 で届いた分ずつ読み、総時間が上限を超えたら打ち切る。
            buf = bytearray()
            while len(buf) < _MAX_BYTES:
                if time.monotonic() - started > _ITEM_TOTAL_S:
                    return None
                chunk = resp.read1(min(_CHUNK, _MAX_BYTES - len(buf)))
                if not chunk:
                    break
                buf += chunk
            return bytes(buf)
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, OSError):
        return None


def extract_body(raw: bytes | str, url: str = "") -> str:
    """HTML から本文を抽出し、空白を正規化して先頭 BODY_MAX_CHARS 字を返す。取れなければ空。"""
    import trafilatura  # 重い依存なので使う時だけ読み込む(他の CLI・テストの起動を遅くしない)

    try:
        text = trafilatura.extract(raw, url=url or None, include_comments=False,
                                   include_tables=False) or ""
    except Exception:  # trafilatura は壊れた HTML で様々な例外を出す。1件の失敗で全体を止めない
        return ""
    return " ".join(text.split())[:BODY_MAX_CHARS]


def _fetch_body(url: str) -> str:
    raw = _download(url)
    if not raw:
        return ""
    return extract_body(raw, url)


def enrich_bodies(candidates: list[dict], budget_s: float = 90) -> int:
    """対象候補の `body` を本文抜粋で埋める(その場で書き換え)。埋まった候補の数を返す。

    同じ URL が複数ジャンルにあっても取得は1回。budget_s を過ぎたら残りは諦めて空のまま返す
    (配信時刻に間に合わせるのが本文より優先)。
    """
    try:
        import trafilatura  # noqa: F401
    except ImportError:
        print("  本文取得: trafilatura が未インストールのため省略 (pip install -r requirements.txt)",
              file=sys.stderr)
        return 0
    by_url: dict[str, list[dict]] = {}
    for c in candidates:
        if is_target(c) and not c.get("body"):
            by_url.setdefault(c["url"], []).append(c)
    if not by_url:
        return 0

    deadline = time.monotonic() + budget_s
    todo: queue.Queue[str] = queue.Queue()
    for url in by_url:
        todo.put(url)
    results: queue.Queue[tuple[str, str]] = queue.Queue()
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            try:
                url = todo.get_nowait()
            except queue.Empty:
                return
            try:
                body = _fetch_body(url)
            except Exception:
                body = ""
            results.put((url, body))

    # daemon にする: ThreadPoolExecutor の実行中ワーカーは Python 終了時に join されるため、
    # budget で戻っても取得中の1件が終わるまでプロセスが終わらず、配信が遅れる。
    for _ in range(min(_WORKERS, len(by_url))):
        threading.Thread(target=worker, daemon=True).start()

    filled = 0
    pending = len(by_url)
    try:
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                url, body = results.get(timeout=remaining)
            except queue.Empty:
                break
            pending -= 1
            if body:
                for c in by_url[url]:
                    c["body"] = body
                    filled += 1
    finally:
        stop.set()  # 時間切れの残りは取りに行かない(取得中のものは待たずに放置する)
    if pending:
        print(f"  本文取得: {budget_s:.0f}秒の上限で {pending} 件を打ち切り", file=sys.stderr)
    return filled
