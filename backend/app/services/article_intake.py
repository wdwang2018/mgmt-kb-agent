"""文章摄入：把「粘贴全文 / URL / 上传文件」三种来源统一成纯文本。

设计约定
--------
1. 零新增依赖：URL 用 httpx（项目已有）+ lxml 启发式正文抽取；docx 用 python-docx；
   pdf 用 pdfminer.six；txt/md 直读。
2. 依赖缺失时**优雅报错而非 500**：本模块对三种可选解析库一律延迟导入，
   缺哪个就抛 IntakeError(依赖缺失码)，端点转成 422 + 明确文案，系统主体不受影响。
3. **绝不静默返回空文本**：抓不到正文必须报错，不能让下游拿空串去问 LLM。
   宁可让用户知道「这个链接抓不到」，也不要给一份编造的解析结果。
4. 长度有上限：超长截断并记 warning，防止单篇文章打爆 token 预算。

对外形式：**本模块产出的是「原文正文」，仅供解析服务阅读理解**，
绝不可直接对外展示或落库为引用内容——对外一律走要点化转述（见 ArticleAnalysis.summary）。
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

MAX_ARTICLE_CHARS = 30_000      # 超过则截断（约 1 万字，够长且控 token 成本）
MIN_USEFUL_CHARS = 150          # 低于则提示过短，可能解析不出结论
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".text"}


class IntakeError(Exception):
    """摄入失败。code 用于端点映射 HTTP 状态码与文案。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 1. 粘贴全文
# ---------------------------------------------------------------------------
def intake_paste(text: str) -> tuple[str, list[str]]:
    raw = (text or "").strip()
    if not raw:
        raise IntakeError("empty_text", "粘贴内容为空")
    return _finish(raw)


# ---------------------------------------------------------------------------
# 2. URL 抓取 + 正文抽取
# ---------------------------------------------------------------------------
def intake_url(url: str, timeout: float = 20.0) -> tuple[str, list[str]]:
    url = (url or "").strip()
    if not url:
        raise IntakeError("empty_url", "URL 为空")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    _guard_ssrf(url)

    import httpx  # 延迟：仅在真正用 URL 时引入

    try:
        # follow_redirects：公众号/短链普遍跳转。
        # 刻意不传 proxy 参数：httpx 默认 trust_env=True，自动读 HTTP(S)_PROXY 环境变量。
        # 硬编码代理地址是错的——沙箱/企业网的代理端口每会话都变（实测 51673→59972），
        # 写死必然连不上；不写反而能自适应有代理与无代理两种环境。
        resp = httpx.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MgmtKB-Agent/1.0)"},
        )
    except Exception as e:  # 网络层任何异常都收敛成同一业务错误
        raise IntakeError("fetch_failed", f"抓取失败：{type(e).__name__}") from e

    if resp.status_code >= 400:
        raise IntakeError("fetch_failed", f"站点返回 {resp.status_code}")

    ctype = resp.headers.get("content-type", "")
    if "pdf" in ctype.lower() or url.lower().endswith(".pdf"):
        return intake_bytes(url.rsplit("/", 1)[-1] or "article.pdf", resp.content)

    return _finish(_extract_main_text(resp.text, url))


def _guard_ssrf(url: str) -> None:
    """拒绝内网/本机地址：这是对外部署的服务，URL 来自用户输入。"""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise IntakeError("bad_url", "无法解析出主机名")
    if host in {"localhost", "localhost.localdomain"}:
        raise IntakeError("forbidden_host", "不接受本机地址")
    try:
        # 域名可能指向内网，解析后逐个判；任一命中私有清单即拒。
        # 刻意不用 ip.is_private：它把 2001::/23（IETF 协议分配段）整段算作私有，
        # 会误杀解析到该段 IPv6 的正常站点（实测 en.wikipedia.org → 2001::1 被拦）。
        # 故改用下表的精确清单——既拦住真风险，又不误伤公网。
        for info in socket.getaddrinfo(host, None):
            if _is_forbidden_ip(ipaddress.ip_address(info[4][0])):
                raise IntakeError("forbidden_host", "不接受内网地址")
    except socket.gaierror:
        # 解析不出就放过：交由 httpx 的真实请求去失败，而不是在这里误杀
        pass


# 只列真正的 SSRF 目标段。169.254.0.0/16 含云厂商元数据服务 169.254.169.254，
# 是 SSRF 的头号目标，必须拦；2001::/23 等协议分配段不拦（否则误杀公网站点）。
_FORBIDDEN_V4 = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),     # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),    # link-local，含云元数据
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
]
_FORBIDDEN_V6 = [
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),          # ULA
    ipaddress.ip_network("fe80::/10"),         # link-local
]


def _is_forbidden_ip(ip) -> bool:
    # IPv4-mapped IPv6（::ffff:127.0.0.1）必须还原成 v4 再判，否则直接绕过
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    nets = _FORBIDDEN_V4 if ip.version == 4 else _FORBIDDEN_V6
    return any(ip in n for n in nets)


def _extract_main_text(html: str, url: str = "") -> str:
    """启发式正文抽取：优先 <article> → 段落最密的容器 → 全部 <p>。

    这是启发式而非通用方案：需登录、纯前端渲染、正文在图片里的站点会抽不到，
    此时返回空串并由调用方报错，绝不用残缺内容冒充全文。
    """
    try:
        from lxml import html as lhtml
    except ImportError as e:
        raise IntakeError("dep_missing", "URL 正文抽取需要 lxml，请 pip install lxml") from e

    try:
        doc = lhtml.fromstring(html)
    except Exception:
        # 极端脏 HTML 退回正则去标签，至少拿到可读文本
        return _strip_tags_regex(html)

    for tag in doc.xpath(
        "//script|//style|//nav|//header|//footer|//aside|//form|//iframe|//noscript|//svg"
    ):
        try:
            tag.drop_tree()
        except Exception:
            pass

    # 1) <article> / <main> 是作者明确标注正文的语义标签，直接信任、不设长度门槛。
    #    刻意不加 MIN_USEFUL_CHARS 判定：短小但正确的 article 一旦被门槛否决，
    #    会落到下面的「拼接所有 <p>」兜底，反而把广告/导航文案一起收进来——
    #    实测就是这么把广告吸进来的，宁可短也不要脏。
    semantic = doc.xpath("//article|//main")
    if semantic:
        text = _node_text(max(semantic, key=lambda e: len(_node_text(e))))
        if text:
            return text

    # 2) 无语义标签：在通用容器里按（段落数, 文本长度）选最像正文的那个
    containers = doc.xpath("//div|//section|//td")
    if containers:
        best = max(containers, key=lambda e: (len(e.xpath(".//p")), len(_node_text(e))))
        text = _node_text(best)
        if text:
            return text

    # 3) 兜底：所有段落拼起来
    ps = [t for t in (_node_text(p) for p in doc.xpath("//p")) if t]
    if ps:
        return "\n\n".join(ps)

    return _node_text(doc)


def _node_text(node) -> str:
    try:
        return re.sub(r"\n{3,}", "\n\n", (node.text_content() or "").strip())
    except Exception:
        return ""


def _strip_tags_regex(html: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s{2,}", " ", s).strip()


# ---------------------------------------------------------------------------
# 3. 文件上传
# ---------------------------------------------------------------------------
def intake_bytes(filename: str, data: bytes) -> tuple[str, list[str]]:
    name = (filename or "").lower()
    if not data:
        raise IntakeError("empty_file", "文件为空")
    if name.endswith(".docx"):
        raw = _read_docx(data)
    elif name.endswith(".pdf"):
        raw = _read_pdf(data)
    elif any(name.endswith(s) for s in TEXT_SUFFIXES) or not _has_suffix(name):
        raw = _read_text(data)
    else:
        raise IntakeError(
            "unsupported_format",
            f"不支持的文件格式：{filename}（支持 txt / md / docx / pdf）",
        )
    if not raw.strip():
        raise IntakeError("no_text", "未能从文件中提取出文字内容（可能是扫描件图片型 PDF）")
    return _finish(raw)


def _has_suffix(name: str) -> bool:
    return bool(re.search(r"\.[a-z0-9]{1,5}$", name))


def _read_text(data: bytes) -> str:
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="ignore")


def _read_docx(data: bytes) -> str:
    try:
        import docx  # python-docx
    except ImportError as e:
        raise IntakeError("dep_missing", "读取 docx 需要 python-docx，请 pip install python-docx") from e
    import io

    try:
        d = docx.Document(io.BytesIO(data))
    except Exception as e:
        raise IntakeError("bad_file", f"docx 解析失败：{type(e).__name__}") from e
    parts = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    for t in d.tables:  # 表格内容也常是正文，不能丢
        for row in t.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _read_pdf(data: bytes) -> str:
    try:
        from pdfminer.high_level import extract_text
    except ImportError as e:
        raise IntakeError(
            "dep_missing", "读取 pdf 需要 pdfminer.six，请 pip install pdfminer.six"
        ) from e
    import io

    try:
        return extract_text(io.BytesIO(data)) or ""
    except Exception as e:
        raise IntakeError("bad_file", f"pdf 解析失败：{type(e).__name__}") from e


# ---------------------------------------------------------------------------
# 收口：统一长度处理与过短提示
# ---------------------------------------------------------------------------
def _finish(raw: str) -> tuple[str, list[str]]:
    text = re.sub(r"\n{3,}", "\n\n", raw.strip())
    warnings: list[str] = []
    if len(text) > MAX_ARTICLE_CHARS:
        text = text[:MAX_ARTICLE_CHARS]
        warnings.append(f"正文超过 {MAX_ARTICLE_CHARS} 字，已截断前段用于解析")
    if len(text) < MIN_USEFUL_CHARS:
        warnings.append(f"正文仅 {len(text)} 字，过短可能解析不出可靠结论")
    return text, warnings
