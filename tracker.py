#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Akademik Konferans Takip Botu
=============================
GitHub'daki açık kaynak AI deadline listelerini (huggingface/ai-deadlines,
ccfddl/ccf-deadlines, paperswithcode/ai-deadlines) ve WikiCFP kategori
sayfalarını tarar; son gönderim tarihine MIN_DAYS–MAX_DAYS gün kalmış
etkinlikleri bulur, tekrarları birleştirir, prestijli olanları işaretler ve
HTML e-posta olarak Gmail üzerinden gönderir.

Yerelde deneme (e-posta göndermez, preview.html üretir):
    DRY_RUN=1 python tracker.py               # Linux / macOS
    $env:DRY_RUN="1"; python tracker.py       # Windows PowerShell
"""
from __future__ import annotations

import html
import io
import json
import logging
import os
import re
import smtplib
import sys
import tarfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ════════════════════════════ AYARLAR ════════════════════════════


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "evet")


MIN_DAYS = _env_int("MIN_DAYS", 10)          # en az kaç gün kalmış olmalı
MAX_DAYS = _env_int("MAX_DAYS", 60)          # en fazla kaç gün kalmış olmalı
MAX_EVENTS = _env_int("MAX_EVENTS", 80)      # e-postadaki azami etkinlik sayısı
DRY_RUN = _env_bool("DRY_RUN", False)        # True: e-posta yok, sadece preview.html
SEND_IF_EMPTY = _env_bool("SEND_IF_EMPTY", False)
WIKICFP_ENABLED = _env_bool("WIKICFP_ENABLED", True)
WIKICFP_PAGES = _env_int("WIKICFP_PAGES", 2)  # kategori başına taranacak sayfa
WIKICFP_DELAY_SEC = 2.0                       # siteye nazik davranmak için bekleme

LOCAL_TZ = ZoneInfo("Europe/Istanbul")
AOE = timezone(timedelta(hours=-12), "AoE")
STATE_FILE = Path(os.getenv("STATE_FILE", "state/seen.json"))
PREVIEW_FILE = Path(os.getenv("PREVIEW_FILE", "preview.html"))
STATE_RETENTION_DAYS = 180

# (görünen ad, GitHub repo). Sıra önemli: aynı etkinlik birden fazla
# kaynakta varsa tarih bilgisi listede önce gelen kaynaktan alınır.
GITHUB_SOURCES = [
    ("HF ai-deadlines", "huggingface/ai-deadlines"),
    ("CCF-DDL", "ccfddl/ccf-deadlines"),
    ("PwC ai-deadlines", "paperswithcode/ai-deadlines"),
]
# ccf-deadlines tüm bilgisayar bilimini kapsar; sadece ilgili alt alanlar:
# AI = yapay zekâ, DB = veri madenciliği/veri bilimi, MX = disiplinlerarası
CCF_CATEGORIES = {"AI", "DB", "MX"}

WIKICFP_CATEGORIES = [
    "artificial intelligence", "machine learning", "deep learning",
    "computer vision", "robotics", "data science", "data mining",
    "remote sensing", "satellite", "space", "aerospace",
    "evolutionary computation", "swarm intelligence",
]
WIKICFP_BASES = ("http://www.wikicfp.com", "https://www.wikicfp.com")

# CORE sıralaması verisi yoksa yine de "üst düzey" sayılacak amiral gemisi
# konferanslar (kısaltma, küçük harf). İstediğin gibi düzenleyebilirsin.
FLAGSHIP_FALLBACK = {
    "neurips", "nips", "icml", "iclr", "cvpr", "iccv", "eccv", "aaai",
    "ijcai", "acl", "emnlp", "naacl", "kdd", "icra", "iros", "rss", "corl",
    "miccai", "gecco",
}

# Başlık / tam ad / not içinde aranacak yayıncı işaretleri (sezgisel).
PUBLISHER_PATTERNS = [
    ("IEEE", re.compile(r"\bIEEE\b")),
    ("ACM", re.compile(r"\bACM\b|\bSIG(?:KDD|IR|MOD|GRAPH|CHI|SPATIAL|EVO)\b")),
    ("Springer", re.compile(r"\bSpringer\b|\bLNCS\b|\bLNAI\b|\bCCIS\b", re.I)),
    ("Elsevier", re.compile(r"\bElsevier\b", re.I)),
    ("CVF", re.compile(r"\bCVF\b")),
]

# Bilinen yağmacı (predatory) yayıncı işaretleri: bu etkinlikler hiç gösterilmez.
BLOCKLIST_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"\bWASET\b", r"World Academy of Science", r"\bAIRCC\b", r"\bIAEME\b",
    )
] + [
    re.compile(re.escape(k.strip()), re.I)
    for k in os.getenv("BLOCK_KEYWORDS", "").split(",") if k.strip()
]

STOIC_QUOTES = [
    ("Seneca", "Her şey başkasınındır Lucilius; yalnızca zaman bizimdir."),
    ("Seneca", "Hangi limana gideceğini bilmeyene hiçbir rüzgâr uygun değildir."),
    ("Seneca", "Yaşamanın en büyük engeli beklentidir: yarına bel bağlar, bugünü kaybettirir."),
    ("Marcus Aurelius", "Sabah isteksizce uyandığında kendine şunu söyle: "
                        "Bir insanın işini yapmak için uyanıyorum."),
    ("Marcus Aurelius", "Her işi, hayatının son işiymiş gibi yap."),
    ("Epiktetos", "Önce kendine ne olmak istediğini söyle; sonra yapman gerekeni yap."),
]

TR_MONTHS = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]
TR_MONTHS_LONG = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
                  "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
TR_DAYS = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]

log = logging.getLogger("tracker")

# ════════════════════════════ VERİ MODELİ ════════════════════════════


@dataclass(eq=False)
class Deadline:
    kind: str            # "abstract" | "full"
    when: datetime       # UTC, timezone-aware
    tz_label: str = ""


@dataclass
class Event:
    key: str
    title: str
    year: int | None
    full_name: str = ""
    link: str = ""
    place: str = ""
    when_str: str = ""
    deadlines: list[Deadline] = field(default_factory=list)
    core_rank: str = ""
    ccf_rank: str = ""
    sources: list[str] = field(default_factory=list)
    publishers: list[str] = field(default_factory=list)
    from_wikicfp: bool = False
    is_new: bool = False
    next_deadline: Deadline | None = None
    days_left: float = 0.0

    @property
    def display_title(self) -> str:
        if self.year and str(self.year) not in self.title:
            return f"{self.title} {self.year}"
        return self.title

    @property
    def base_acronym(self) -> str:
        return re.sub(r"[^a-z]", "", re.sub(r"\b(19|20)\d{2}\b", "", self.title.lower()))

    @property
    def is_flagship(self) -> bool:
        return self.base_acronym in FLAGSHIP_FALLBACK

    @property
    def prestige(self) -> int:
        """3: A* / amiral gemisi, 2: CORE A, 1: IEEE/ACM/Springer..., 0: diğer."""
        if self.core_rank == "A*" or self.is_flagship:
            return 3
        if self.core_rank == "A":
            return 2
        if self.publishers:
            return 1
        return 0


# ════════════════════════════ YARDIMCILAR ════════════════════════════


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3, connect=3, read=3, backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]), raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (compatible; AcademicConferenceTracker/1.0; personal, non-commercial)"
    )
    return session


def make_key(title: str, year: int | None) -> str:
    base = re.sub(r"\b(19|20)\d{2}\b", "", title.lower())
    base = re.sub(r"[^a-z0-9]", "", base)
    return f"{base}-{year or ''}"


def to_int(value: Any) -> int | None:
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


def unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


_TZ_ABBR = {
    "PST": -8, "PDT": -7, "MST": -7, "MDT": -6, "CST": -6, "CDT": -5, "EST": -5,
    "EDT": -4, "CET": 1, "CEST": 2, "BST": 1, "IST": 5.5, "JST": 9, "KST": 9,
    "SGT": 8, "HKT": 8, "AEST": 10, "AEDT": 11, "TRT": 3,
}


def parse_tz(value: Any) -> tuple[tzinfo, str]:
    """'AoE', 'UTC-12', 'GMT+8', 'PST', 'America/New_York' gibi değerleri çözer."""
    if value is None or str(value).strip() == "":
        return timezone.utc, "UTC"
    raw = str(value).strip()
    up = raw.upper().replace(" ", "")
    if up in ("AOE", "ANYWHEREONEARTH"):
        return AOE, "AoE"
    if up in ("UTC", "GMT", "Z"):
        return timezone.utc, "UTC"
    match = re.fullmatch(r"(?:UTC|GMT)([+-])(\d{1,2})(?::?(\d{2}))?", up)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        hours, minutes = int(match.group(2)), int(match.group(3) or 0)
        if hours <= 14:
            if sign == -1 and hours == 12 and minutes == 0:
                return AOE, "AoE"
            return timezone(sign * timedelta(hours=hours, minutes=minutes)), raw
    if up in _TZ_ABBR:
        return timezone(timedelta(hours=_TZ_ABBR[up])), raw
    try:
        return ZoneInfo(raw), raw
    except (ZoneInfoNotFoundError, ValueError):
        log.debug("Bilinmeyen saat dilimi %r, UTC varsayıldı", raw)
        return timezone.utc, "UTC"


_TBA = re.compile(r"\b(tba|tbd|n/?a|none|unknown|null)\b|\?", re.I)
_STRICT_FORMATS = (
    ("%Y-%m-%d %H:%M:%S", False), ("%Y-%m-%d %H:%M", False),
    ("%Y-%m-%dT%H:%M:%S", False), ("%Y-%m-%dT%H:%M", False),
    ("%Y-%m-%d", True), ("%Y/%m/%d", True),
    ("%b %d, %Y", True), ("%B %d, %Y", True), ("%d %B %Y", True),
)


def parse_deadline_value(value: Any, tz: tzinfo) -> datetime | None:
    """Her türlü tarih değerini UTC datetime'a çevirir; çözemezse None döner."""
    try:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, datetime):
            dt = value if value.tzinfo else value.replace(tzinfo=tz)
            return dt.astimezone(timezone.utc)
        if isinstance(value, date):  # YAML tırnaksız tarihleri date'e çevirir
            return datetime(value.year, value.month, value.day, 23, 59, 59,
                            tzinfo=tz).astimezone(timezone.utc)
        text = str(value).strip()
        if not text or _TBA.search(text):
            return None
        for fmt, date_only in _STRICT_FORMATS:
            try:
                dt = datetime.strptime(text, fmt)
            except ValueError:
                continue
            if date_only:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt.replace(tzinfo=tz).astimezone(timezone.utc)
        dt = dateparser.parse(text, default=datetime(1900, 1, 1, 23, 59, 59))
        if dt is None or dt.year < 2000:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError, TypeError) as exc:
        log.debug("Tarih çözülemedi %r: %s", value, exc)
        return None


def _norm_rank(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if text in ("A*", "A", "B", "C") else ""


def extract_rank(item: Any, which: str) -> str:
    """which='core' veya 'ccf'. Farklı şemalardaki sıralama alanlarını tolere eder."""
    if not isinstance(item, dict):
        return ""
    # Not: "era_rating" bilerek kullanılmıyor; eski ERA listesi CORE ile aynı değil.
    keys = ["rank", "rankings", "ranking", which]
    for key in keys:
        value = item.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, dict):
            for sub_key, sub_val in value.items():
                if which in str(sub_key).lower():
                    rank = _norm_rank(sub_val)
                    if rank:
                        return rank
            continue
        if key == which:
            rank = _norm_rank(value)
            if rank:
                return rank
        match = re.search(rf"{which}\W*(A\*|A|B|C)(?![A-Za-z])", str(value), re.I)
        if match:
            return match.group(1).upper()
    return ""


_SKIP_DEADLINE = re.compile(
    r"notif|camera|rebuttal|review|response|decision|conference|event|"
    r"start|end|supplement|early.?bird|registration(?! of abstract)", re.I)


def _dedupe_deadlines(items: list[Deadline]) -> list[Deadline]:
    seen: set[tuple[str, str]] = set()
    out = []
    for d in sorted(items, key=lambda x: x.when):
        sig = (d.kind, d.when.strftime("%Y-%m-%d %H:%M"))
        if sig not in seen:
            seen.add(sig)
            out.append(d)
    return out


def deadlines_from_item(item: dict) -> list[Deadline]:
    base_tz, base_label = parse_tz(item.get("timezone"))
    out: list[Deadline] = []

    def add(kind: str, value: Any, tz_value: Any = None) -> None:
        tz, label = parse_tz(tz_value) if tz_value else (base_tz, base_label)
        for v in value if isinstance(value, list) else [value]:
            if isinstance(v, dict):
                continue
            dt = parse_deadline_value(v, tz)
            if dt:
                out.append(Deadline(kind, dt, label))

    add("abstract", item.get("abstract_deadline"))
    add("full", item.get("deadline"))

    for d in item.get("deadlines") or []:          # yeni HF şeması
        if not isinstance(d, dict):
            continue
        text = f"{d.get('type', '')} {d.get('label', '')}".lower()
        if "abstract" in text and "notif" not in text:
            kind = "abstract"
        elif _SKIP_DEADLINE.search(text):
            continue
        else:
            kind = "full"
        add(kind, d.get("date") or d.get("deadline"), d.get("timezone"))

    for t in item.get("timeline") or []:           # ccf-deadlines şeması
        if isinstance(t, dict):
            add("abstract", t.get("abstract_deadline"))
            add("full", t.get("deadline"))
    return _dedupe_deadlines(out)


def detect_publishers(*texts: str) -> list[str]:
    blob = " ".join(t for t in texts if t)
    return [name for name, pattern in PUBLISHER_PATTERNS if pattern.search(blob)]


def is_blocked(event: Event) -> bool:
    blob = f"{event.title} {event.full_name}"
    return any(p.search(blob) for p in BLOCKLIST_PATTERNS)


# ════════════════════════════ KAYNAK 1: GITHUB ════════════════════════════


def event_from_item(item: dict, source: str, parent: dict | None = None) -> Event | None:
    parent = parent or {}
    title = str(item.get("title") or parent.get("title") or "").strip()
    if not title:
        return None
    deadlines = deadlines_from_item(item)
    if not deadlines:
        return None
    year = to_int(item.get("year")) or to_int(title)
    full_name = str(item.get("full_name") or parent.get("description")
                    or item.get("description") or "").strip()
    place = item.get("place") or ", ".join(
        str(x) for x in (item.get("city"), item.get("country")) if x
    ) or item.get("venue") or ""
    note = str(item.get("note") or "")
    return Event(
        key=make_key(title, year),
        title=title,
        year=year,
        full_name=full_name,
        link=str(item.get("link") or "").strip(),
        place=str(place).strip(),
        when_str=str(item.get("date") or "").strip(),
        deadlines=deadlines,
        core_rank=extract_rank(item, "core") or extract_rank(parent, "core"),
        ccf_rank=extract_rank(item, "ccf") or extract_rank(parent, "ccf"),
        sources=[source],
        publishers=detect_publishers(title, full_name, note),
    )


def walk_yaml(data: Any, source: str) -> Iterator[Event]:
    if isinstance(data, dict):
        if any(k in data for k in ("confs", "deadline", "deadlines", "abstract_deadline")):
            data = [data]
        else:
            data = next((v for v in data.values() if isinstance(v, list)), [])
    if not isinstance(data, list):
        return
    for item in data:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("confs"), list):          # ccf-deadlines: seri + yıllar
            for conf in item["confs"]:
                if isinstance(conf, dict):
                    event = event_from_item(conf, source, parent=item)
                    if event:
                        yield event
        else:                                            # ai-deadlines
            event = event_from_item(item, source)
            if event:
                yield event


def _yaml_path_ok(repo: str, path: str) -> bool:
    parts = path.split("/")
    low = path.lower()
    if repo.startswith("ccfddl/"):
        return (len(parts) >= 3 and parts[-3].lower() == "conference"
                and parts[-2].upper() in CCF_CATEGORIES)
    return ("conference" in low and "/.github/" not in low
            and "node_modules" not in low)


def fetch_github_repo(session: requests.Session, name: str, repo: str) -> list[Event]:
    """Repoyu tek istekte tarball olarak indirir; dosya yolu değişse bile YAML'ları bulur.
    Önce API limiti olmayan codeload denenir, olmazsa GitHub API'ye düşülür."""
    api_headers = {"Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        api_headers["Authorization"] = f"Bearer {token}"
    attempts = [
        (f"https://codeload.github.com/{repo}/tar.gz/HEAD", {}),
        (f"https://api.github.com/repos/{repo}/tarball", api_headers),
    ]
    content = None
    last_error: Exception | None = None
    for url, headers in attempts:
        try:
            resp = session.get(url, headers=headers, timeout=120)
            resp.raise_for_status()
            content = resp.content
            break
        except requests.RequestException as exc:
            last_error = exc
            log.debug("%s indirilemedi (%s): %s", name, url, exc)
    if content is None:
        raise RuntimeError(f"tarball indirilemedi: {last_error}")

    events: list[Event] = []
    files = 0
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or member.size > 5_000_000:
                continue
            if not member.name.lower().endswith((".yml", ".yaml")):
                continue
            if not _yaml_path_ok(repo, member.name):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            try:
                data = yaml.safe_load(handle.read())
            except yaml.YAMLError as exc:
                log.debug("%s: %s okunamadı: %s", name, member.name, exc)
                continue
            files += 1
            events.extend(walk_yaml(data, name))
    log.info("%s: %d YAML dosyası, %d kayıt", name, files, len(events))
    if files == 0:
        raise RuntimeError("repoda konferans YAML'ı bulunamadı")
    return events


# ════════════════════════════ KAYNAK 2: WIKICFP ════════════════════════════

_WCFP_DATE = re.compile(r"[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4}")


def parse_wikicfp_html(page_html: str, base: str, category: str) -> list[Event]:
    """WikiCFP kategori sayfası: her etkinlik iki satır (başlık / When-Where-Deadline)."""
    soup = BeautifulSoup(page_html, "html.parser")
    out: list[Event] = []
    for anchor in soup.select('a[href*="event.showcfp"]'):
        row = anchor.find_parent("tr")
        info_row = row.find_next_sibling("tr") if row else None
        if row is None or info_row is None:
            continue
        acronym = anchor.get_text(" ", strip=True)
        cells = row.find_all("td")
        full_name = cells[1].get_text(" ", strip=True) if len(cells) > 1 else ""
        info = [td.get_text(" ", strip=True) for td in info_row.find_all("td")]
        if len(info) < 3 or not acronym:
            continue
        when_str, place, deadline_str = info[0], info[1], info[2]
        dates = _WCFP_DATE.findall(deadline_str)
        if not dates:
            continue
        # WikiCFP saat dilimi vermez → güvenli tarafta kalmak için TSİ gün sonu
        deadlines = []
        full_dt = parse_deadline_value(dates[0].replace(".", ""), LOCAL_TZ)
        if full_dt:
            deadlines.append(Deadline("full", full_dt, "WikiCFP"))
        if len(dates) > 1 and "(" in deadline_str:
            abs_dt = parse_deadline_value(dates[1].replace(".", ""), LOCAL_TZ)
            if abs_dt:
                deadlines.append(Deadline("abstract", abs_dt, "WikiCFP"))
        if not deadlines:
            continue
        year = to_int(acronym) or to_int(when_str) or deadlines[0].when.year
        out.append(Event(
            key=make_key(acronym, year),
            title=re.sub(r"\s+", " ", acronym),
            year=year,
            full_name=full_name,
            link=urljoin(base, anchor.get("href", "")),
            place=place if place.upper() != "N/A" else "",
            when_str=when_str if when_str.upper() != "N/A" else "",
            deadlines=_dedupe_deadlines(deadlines),
            sources=[f"WikiCFP · {category}"],
            publishers=detect_publishers(acronym, full_name),
            from_wikicfp=True,
        ))
    return out


def fetch_wikicfp(session: requests.Session) -> list[Event]:
    events: list[Event] = []
    ok_pages = 0
    base = WIKICFP_BASES[0]
    for category in WIKICFP_CATEGORIES:
        for page in range(1, WIKICFP_PAGES + 1):
            path = f"/cfp/call?conference={quote(category)}&page={page}"
            text = None
            for candidate in (base,) + tuple(b for b in WIKICFP_BASES if b != base):
                try:
                    resp = session.get(candidate + path, timeout=30)
                    resp.raise_for_status()
                    text, base = resp.text, candidate
                    break
                except requests.RequestException as exc:
                    log.warning("WikiCFP %r s.%d (%s): %s", category, page, candidate, exc)
            time.sleep(WIKICFP_DELAY_SEC)
            if text is None:
                break
            ok_pages += 1
            page_events = parse_wikicfp_html(text, base, category)
            events.extend(page_events)
            if not page_events:
                break
    log.info("WikiCFP: %d sayfa, %d kayıt", ok_pages, len(events))
    if ok_pages == 0:
        raise RuntimeError("hiçbir WikiCFP sayfasına erişilemedi")
    return events


# ════════════════════════════ BİRLEŞTİRME & FİLTRE ════════════════════════════

_RANK_ORDER = {"A*": 4, "A": 3, "B": 2, "C": 1, "": 0}


def merge_events(events: list[Event]) -> list[Event]:
    merged: dict[str, Event] = {}
    for ev in events:
        cur = merged.get(ev.key)
        if cur is None:
            merged[ev.key] = ev
            continue
        cur.sources = unique(cur.sources + ev.sources)
        kinds = {d.kind for d in cur.deadlines}
        cur.deadlines = _dedupe_deadlines(
            cur.deadlines + [d for d in ev.deadlines if d.kind not in kinds])
        if _RANK_ORDER[ev.core_rank] > _RANK_ORDER[cur.core_rank]:
            cur.core_rank = ev.core_rank
        if _RANK_ORDER[ev.ccf_rank] > _RANK_ORDER[cur.ccf_rank]:
            cur.ccf_rank = ev.ccf_rank
        if len(ev.full_name) > len(cur.full_name):
            cur.full_name = ev.full_name
        cur.link = cur.link or ev.link
        cur.place = cur.place or ev.place
        cur.when_str = cur.when_str or ev.when_str
        cur.publishers = unique(cur.publishers + ev.publishers)
        cur.from_wikicfp = cur.from_wikicfp and ev.from_wikicfp
    return list(merged.values())


def select_events(events: list[Event], now: datetime) -> list[Event]:
    picked = []
    for ev in events:
        if is_blocked(ev):
            continue
        window = [(d, (d.when - now).total_seconds() / 86400) for d in ev.deadlines]
        window = [(d, days) for d, days in window if MIN_DAYS <= days <= MAX_DAYS]
        if not window:
            continue
        ev.next_deadline, ev.days_left = min(window, key=lambda t: t[1])
        picked.append(ev)
    picked.sort(key=lambda e: (e.days_left, -e.prestige, e.title.lower()))
    return picked[:MAX_EVENTS]


# ════════════════════════════ DURUM (TEKRAR TAKİBİ) ════════════════════════════


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"events": {}}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Durum dosyası okunamadı (%s), sıfırdan başlanıyor", exc)
        return {"events": {}}
    if not isinstance(data, dict) or not isinstance(data.get("events"), dict):
        return {"events": {}}
    return data


def save_state(state: dict, picked: list[Event], now: datetime) -> None:
    events = state.setdefault("events", {})
    today = now.date().isoformat()
    for ev in picked:
        entry = events.setdefault(ev.key, {"first_seen": today, "title": ev.display_title})
        entry["last_seen"] = today
    cutoff = (now - timedelta(days=STATE_RETENTION_DAYS)).date().isoformat()
    state["events"] = {
        k: v for k, v in events.items()
        if isinstance(v, dict) and str(v.get("last_seen", v.get("first_seen", ""))) >= cutoff
    }
    state["last_run"] = now.isoformat(timespec="seconds")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
                          encoding="utf-8")


# ════════════════════════════ E-POSTA ŞABLONU ════════════════════════════

esc = html.escape


def fmt_tr(dt: datetime) -> str:
    loc = dt.astimezone(LOCAL_TZ)
    return f"{loc.day} {TR_MONTHS[loc.month - 1]} {loc.year} {TR_DAYS[loc.weekday()]}, {loc:%H:%M}"


def _badge(text: str, bg: str, fg: str) -> str:
    return (f'<span style="display:inline-block;background:{bg};color:{fg};'
            f'font-size:11px;font-weight:700;padding:3px 9px;border-radius:999px;'
            f'margin:0 4px 4px 0;letter-spacing:.2px">{esc(text)}</span>')


def _urgency_color(days: float) -> str:
    days = int(days)  # kartta gösterilen tam gün sayısıyla tutarlı olsun
    if days <= 14:
        return "#dc2626"
    if days <= 30:
        return "#ea580c"
    return "#16a34a"


def _badges(ev: Event) -> str:
    parts = []
    if ev.is_new:
        parts.append(_badge("🆕 YENİ", "#dcfce7", "#166534"))
    if ev.core_rank == "A*":
        parts.append(_badge("⭐ CORE A*", "#fde68a", "#92400e"))
    elif ev.is_flagship:
        parts.append(_badge("🏆 Üst düzey", "#ede9fe", "#5b21b6"))
    if ev.core_rank in ("A", "B", "C"):
        colors = {"A": ("#dbeafe", "#1e40af"), "B": ("#e2e8f0", "#334155"),
                  "C": ("#f1f5f9", "#64748b")}[ev.core_rank]
        parts.append(_badge(f"CORE {ev.core_rank}", *colors))
    if ev.ccf_rank:
        parts.append(_badge(f"CCF {ev.ccf_rank}", "#f1f5f9", "#475569"))
    for pub in ev.publishers:
        parts.append(_badge(pub, "#e0e7ff", "#3730a3"))
    if ev.from_wikicfp and ev.prestige == 0:
        parts.append(_badge("🔎 WikiCFP · doğrula", "#fef3c7", "#78350f"))
    return "".join(parts)


def _event_card(ev: Event, now: datetime) -> str:
    accent, background = {3: ("#f59e0b", "#fffbeb"), 2: ("#3b82f6", "#f8fbff"),
                          1: ("#6366f1", "#ffffff"), 0: ("#cbd5e1", "#ffffff")}[ev.prestige]
    title_html = esc(ev.display_title)
    if ev.link:
        title_html = (f'<a href="{esc(ev.link, quote=True)}" style="color:#0f172a;'
                      f'text-decoration:none">{title_html} ↗</a>')
    meta = " · ".join(x for x in (
        f"📍 {esc(ev.place)}" if ev.place else "",
        f"🗓️ {esc(ev.when_str)}" if ev.when_str else "") if x)

    rows = []
    upcoming = [d for d in ev.deadlines if d.when >= now]
    # Aylık/yuvarlanan son tarihli etkinliklerde kartı şişirmemek için ilk 3 tarih
    shown = sorted(set(upcoming[:3]) | {ev.next_deadline}, key=lambda x: x.when)
    for d in shown:
        days = (d.when - now).total_seconds() / 86400
        label = "Özet (abstract)" if d.kind == "abstract" else "Tam bildiri"
        is_next = d is ev.next_deadline
        color = _urgency_color(days) if is_next else "#64748b"
        weight = "700" if is_next else "400"
        tz_note = f" · kaynak: {esc(d.tz_label)}" if d.tz_label and d.tz_label != "WikiCFP" else ""
        rows.append(
            f'<div style="font-size:13px;color:#334155;margin-top:4px;font-weight:{weight}">'
            f'{"⏳" if is_next else "•"} {label}: {fmt_tr(d.when)} TSİ'
            f'<span style="color:#94a3b8;font-weight:400">{tz_note}</span> '
            f'<span style="color:{color}">— {int(days)} gün</span></div>')

    pill_color = _urgency_color(ev.days_left)
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 14px;
  border:1px solid #e2e8f0;border-left:5px solid {accent};border-radius:10px;background:{background}">
 <tr><td style="padding:14px 16px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
   <td style="vertical-align:top">{_badges(ev)}</td>
   <td style="vertical-align:top;text-align:right;white-space:nowrap">
    <span style="display:inline-block;background:{pill_color};color:#fff;font-weight:800;
      font-size:13px;padding:5px 11px;border-radius:999px">{int(ev.days_left)} gün</span></td>
  </tr></table>
  <div style="font-size:17px;font-weight:800;margin-top:4px">{title_html}</div>
  {f'<div style="font-size:13px;color:#475569;margin-top:2px">{esc(ev.full_name)}</div>'
   if ev.full_name and ev.full_name.lower() != ev.title.lower() else ''}
  {f'<div style="font-size:12px;color:#64748b;margin-top:6px">{meta}</div>' if meta else ''}
  <div style="margin-top:8px">{''.join(rows)}</div>
  <div style="font-size:11px;color:#94a3b8;margin-top:8px">Kaynak: {esc(', '.join(ev.sources))}</div>
 </td></tr>
</table>"""


def render_email(picked: list[Event], status: list[tuple[str, bool, str]],
                 now: datetime) -> tuple[str, str, str]:
    local_now = now.astimezone(LOCAL_TZ)
    n_new = sum(e.is_new for e in picked)
    n_top = sum(e.prestige >= 2 for e in picked)
    n_urgent = sum(int(e.days_left) <= 14 for e in picked)
    date_label = f"{local_now.day} {TR_MONTHS_LONG[local_now.month - 1]} {local_now.year}"

    subject = f"🎯 {len(picked)} konferans fırsatı"
    if n_new:
        subject += f" · {n_new} yeni"
    if n_top:
        subject += f" · {n_top} üst düzey"
    subject += f" — {date_label}"

    author, quote_text = STOIC_QUOTES[local_now.toordinal() % len(STOIC_QUOTES)]

    def stat(value: int, label: str, color: str) -> str:
        return (f'<td align="center" style="padding:10px 4px"><div style="font-size:24px;'
                f'font-weight:800;color:{color}">{value}</div><div style="font-size:11px;'
                f'color:#64748b;text-transform:uppercase;letter-spacing:.5px">{label}</div></td>')

    if picked:
        cards = "".join(_event_card(e, now) for e in picked)
    else:
        cards = ('<p style="font-size:15px;color:#475569;text-align:center;padding:24px 0">'
                 'Bu aralıkta yeni bir son tarih yok. Araştırmaya odaklanmak için harika bir hafta! 🔬</p>')

    status_html = " · ".join(
        f'{"✅" if ok else "⚠️"} {esc(name)} ({esc(info)})' for name, ok, info in status)

    body = f"""<!DOCTYPE html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(subject)}</title></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9">
<tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:680px">
 <tr><td style="background:#0f172a;background-image:linear-gradient(135deg,#0f172a,#1e3a8a);
   border-radius:14px 14px 0 0;padding:26px 24px;color:#fff">
  <div style="font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#93c5fd">
   Akademik Konferans Radarı · {esc(date_label)}</div>
  <div style="font-size:24px;font-weight:800;margin-top:6px">🚀 Bu dönemin fırsatları</div>
  <div style="font-size:13px;color:#cbd5e1;margin-top:6px">
   Son başvuruya {MIN_DAYS}–{MAX_DAYS} gün kalan etkinlikler, en yakından en uzağa.</div>
 </td></tr>
 <tr><td style="background:#fff;padding:6px 12px;border-bottom:1px solid #e2e8f0">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
   {stat(len(picked), "Toplam", "#0f172a")}{stat(n_new, "Yeni", "#16a34a")}
   {stat(n_top, "Üst düzey", "#d97706")}{stat(n_urgent, "≤14 gün", "#dc2626")}
  </tr></table>
 </td></tr>
 <tr><td style="background:#fff;padding:18px 20px 4px">
  <div style="border-left:4px solid #f59e0b;background:#fffbeb;padding:12px 14px;border-radius:6px;
    font-size:14px;color:#78350f;font-style:italic">“{esc(quote_text)}”
   <div style="font-style:normal;font-size:12px;margin-top:6px;color:#92400e">— {esc(author)}</div></div>
 </td></tr>
 <tr><td style="background:#fff;padding:16px 20px 8px">{cards}</td></tr>
 <tr><td style="background:#f8fafc;border-radius:0 0 14px 14px;padding:16px 20px;font-size:11px;
   color:#64748b;line-height:1.6;border-top:1px solid #e2e8f0">
  <b>Rozetler:</b> ⭐ CORE A* / 🏆 bilinen amiral gemisi konferans · CORE A/B/C ·
  IEEE/ACM/Springer adı geçenler (sezgisel tespit) · 🔎 WikiCFP kayıtlarını başvurmadan önce
  <a href="https://portal.core.edu.au/conf-ranks/" style="color:#2563eb">CORE portalında</a>
  ve yayıncı sitesinde doğrula.<br>
  <b>Kaynak durumu:</b> {status_html}<br>
  Saatler Türkiye saatine (TSİ) çevrilmiştir. Bu e-posta GitHub Actions ile otomatik üretildi.
 </td></tr>
</table></td></tr></table></body></html>"""

    lines = [subject, "", f"“{quote_text}” — {author}", ""]
    for e in picked:
        flags = " ".join(f for f in (
            "[YENİ]" if e.is_new else "", f"[CORE {e.core_rank}]" if e.core_rank else "",
            "[ÜST DÜZEY]" if e.is_flagship and e.core_rank != "A*" else "",
            *(f"[{p}]" for p in e.publishers)) if f)
        nd = e.next_deadline
        kind = "Özet" if nd and nd.kind == "abstract" else "Tam bildiri"
        lines.append(f"- {e.display_title} {flags}".rstrip())
        if nd:
            lines.append(f"  {kind}: {fmt_tr(nd.when)} TSİ ({int(e.days_left)} gün)")
        if e.link:
            lines.append(f"  {e.link}")
    lines += ["", "Kaynaklar: " + ", ".join(f"{n} {'OK' if ok else 'HATA'}" for n, ok, _ in status)]
    return subject, body, "\n".join(lines)


def send_email(subject: str, html_body: str, text_body: str) -> None:
    sender = os.getenv("GMAIL_ADDRESS", "").strip()
    password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if not sender or not password:
        raise RuntimeError("GMAIL_ADDRESS ve GMAIL_APP_PASSWORD ortam değişkenleri gerekli "
                           "(GitHub Secrets'ı kontrol et).")
    recipients = [r.strip() for r in (os.getenv("RECIPIENT_EMAIL") or sender).split(",")
                  if r.strip()]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Konferans Radarı", sender))
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    for attempt in range(1, 4):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
                smtp.login(sender, password)
                smtp.send_message(msg)
            log.info("E-posta gönderildi → %s", ", ".join(recipients))
            return
        except smtplib.SMTPAuthenticationError as exc:
            raise RuntimeError("Gmail girişi reddedildi: App Password'ü ve 2 adımlı "
                               "doğrulamanın açık olduğunu kontrol et.") from exc
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("SMTP denemesi %d başarısız: %s", attempt, exc)
            time.sleep(5 * attempt)
    raise RuntimeError("E-posta 3 denemede gönderilemedi.")


# ════════════════════════════ ANA AKIŞ ════════════════════════════


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    now = datetime.now(timezone.utc)
    session = make_session()
    collected: list[Event] = []
    status: list[tuple[str, bool, str]] = []

    for name, repo in GITHUB_SOURCES:
        try:
            events = fetch_github_repo(session, name, repo)
            collected.extend(events)
            status.append((name, True, f"{len(events)} kayıt"))
        except Exception as exc:  # noqa: BLE001 — bir kaynak düşse de bot çalışmaya devam eder
            log.warning("%s alınamadı: %s", name, exc)
            status.append((name, False, type(exc).__name__))

    if WIKICFP_ENABLED:
        try:
            events = fetch_wikicfp(session)
            collected.extend(events)
            status.append(("WikiCFP", True, f"{len(events)} kayıt"))
        except Exception as exc:  # noqa: BLE001
            log.warning("WikiCFP alınamadı: %s", exc)
            status.append(("WikiCFP", False, type(exc).__name__))

    if not any(ok for _, ok, _ in status):
        log.error("Hiçbir kaynağa ulaşılamadı; e-posta gönderilmiyor.")
        return 2

    merged = merge_events(collected)
    picked = select_events(merged, now)
    state = load_state()
    known = state.get("events", {})
    for ev in picked:
        ev.is_new = ev.key not in known
    log.info("%d benzersiz etkinlik, %d tanesi %d–%d gün aralığında (%d yeni)",
             len(merged), len(picked), MIN_DAYS, MAX_DAYS, sum(e.is_new for e in picked))

    subject, html_body, text_body = render_email(picked, status, now)
    PREVIEW_FILE.write_text(html_body, encoding="utf-8")
    log.info("Önizleme yazıldı: %s", PREVIEW_FILE.resolve())

    if DRY_RUN:
        log.info("DRY_RUN açık: e-posta gönderilmedi. Konu: %s", subject)
        return 0
    if picked or SEND_IF_EMPTY:
        send_email(subject, html_body, text_body)
    else:
        log.info("Aralıkta etkinlik yok; e-posta atlanıyor.")
    save_state(state, picked, now)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as err:
        log.error("%s", err)
        sys.exit(1)
