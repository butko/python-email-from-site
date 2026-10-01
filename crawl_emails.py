#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Рекурсивный обход сайта с целью сбора e-mail адресов.

Запуск:
    python3 crawl_emails.py [путь_к_config.ini]

Зависимости:
    sudo apt install python3 python3-requests python3-bs4
"""

import os
import re
import sys
import time
import configparser
from collections import defaultdict
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Регулярное выражение для поиска e-mail адресов
EMAIL_REGEX = re.compile(
    r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; EmailCrawler/1.0)'
}

# Небольшая пауза между запросами, чтобы не нагружать сервер
REQUEST_DELAY = 0.3

# Значения по умолчанию, если в конфиге не задан skip_extensions
DEFAULT_SKIP_EXTENSIONS = [
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx',
    'zip', 'rar', '7z', 'tar', 'gz',
    'jpg', 'jpeg', 'png', 'gif', 'svg', 'webp', 'bmp', 'ico',
    'mp3', 'mp4', 'avi', 'mov', 'wmv', 'flv', 'mkv',
    'exe', 'dmg', 'iso', 'apk',
]


# ---------------------------------------------------------------------------
# Чтение конфигурации
# ---------------------------------------------------------------------------

def read_config(config_path: str):
    """
    Читает INI-файл и возвращает:
        url, depth, stay_in_domain, include_subdomains,
        report_file, skip_extensions
    """
    config = configparser.ConfigParser()

    if not config.read(config_path, encoding='utf-8'):
        print(f"Ошибка: не удалось прочитать файл конфигурации '{config_path}'")
        sys.exit(1)

    if not config.has_section('settings'):
        print("Ошибка: в файле конфигурации отсутствует секция [settings]")
        sys.exit(1)

    section = config['settings']

    # URL
    url = section.get('url', '').strip()
    if not url:
        print("Ошибка: не задан параметр 'url' в секции [settings]")
        sys.exit(1)
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    # Глубина
    try:
        depth = int(section.get('depth', '0').strip())
        if depth < 0:
            raise ValueError
    except ValueError:
        print("Ошибка: параметр 'depth' должен быть целым неотрицательным числом")
        sys.exit(1)

    # Оставаться в домене
    stay_raw = section.get('stay_in_domain', 'yes').strip().lower()
    stay_in_domain = stay_raw in ('yes', 'true', '1', 'y', 'да')

    # Учитывать поддомены (актуально только при stay_in_domain = yes)
    sub_raw = section.get('include_subdomains', 'yes').strip().lower()
    include_subdomains = sub_raw in ('yes', 'true', '1', 'y', 'да')

    # Файл отчёта (базовое имя; таймстамп добавится позже)
    report_file = section.get('report_file', 'email_report.txt').strip() \
        or 'email_report.txt'

    # Расширения, которые не нужно скачивать.
    # Используем `in`, а не section.has_option — это совместимо со всеми
    # версиями Python 3 (SectionProxy не имеет has_option).
    if 'skip_extensions' in section:
        raw = section.get('skip_extensions', '')
        if raw.strip() == '':
            # Явно указано: ничего не фильтровать
            skip_extensions = set()
        else:
            skip_extensions = {
                e.strip().lstrip('.').lower()
                for e in raw.split(',')
                if e.strip()
            }
    else:
        # Параметр не задан — используем безопасный набор по умолчанию
        skip_extensions = {e.lower() for e in DEFAULT_SKIP_EXTENSIONS}

    return (url, depth, stay_in_domain, include_subdomains,
            report_file, skip_extensions)


def build_report_filename(base_name: str) -> str:
    """
    Добавляет таймстамп ГГГГ-ММ-ДД-ЧЧ-ММ к имени файла отчёта.
    Пример: 'email_report.txt' -> 'email_report-2025-01-15-14-37.txt'
    """
    timestamp = datetime.now().strftime('%Y-%m-%d-%H-%M')
    base, ext = os.path.splitext(base_name)
    if not ext:
        ext = '.txt'
    return f"{base}-{timestamp}{ext}"


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Убирает фрагмент (#...) — он не влияет на содержимое страницы."""
    parsed = urlparse(url)
    return parsed._replace(fragment='').geturl()


def get_host(url: str) -> str:
    """
    Возвращает host без ведущего www.
    'www.donntu.ru' -> 'donntu.ru'
    'fier.donntu.ru' -> 'fier.donntu.ru'
    """
    host = urlparse(url).netloc.lower()
    # Отрезаем возможный порт (host:port)
    if ':' in host:
        host = host.split(':', 1)[0]
    if host.startswith('www.'):
        host = host[4:]
    return host


def same_domain(url: str, base_host: str,
                include_subdomains: bool = True) -> bool:
    """
    Проверяет, что URL относится к базовому домену.

    include_subdomains = True  → разрешены домен и его поддомены
                                  (donntu.ru, fier.donntu.ru, ...)
    include_subdomains = False → разрешён только сам домен
                                  (donntu.ru и www.donntu.ru, но НЕ fier.donntu.ru)
    """
    host = get_host(url)

    if host == base_host:
        return True
    if include_subdomains and host.endswith('.' + base_host):
        return True
    return False


def url_has_skipped_extension(url: str, skip_extensions: set) -> bool:
    """
    Проверяет расширение последнего сегмента пути URL.
    Пример: '/files/report.PDF?x=1' -> 'pdf' -> True (если pdf в skip_extensions)
    """
    if not skip_extensions:
        return False
    path = urlparse(url).path.lower()
    last_segment = path.rsplit('/', 1)[-1]
    if '.' not in last_segment:
        return False
    ext = last_segment.rsplit('.', 1)[-1]
    return ext in skip_extensions


def response_is_skipped(ctype: str, skip_extensions: set) -> bool:
    """
    Проверяет Content-Type ответа. Мы не хотим качать ничего, кроме HTML/XHTML.
    """
    ctype = (ctype or '').lower()

    if 'text/html' not in ctype and 'application/xhtml' not in ctype:
        return True

    return False


def extract_emails(soup: BeautifulSoup, html_text: str) -> set:
    """
    Извлекает e-mail адреса со страницы:
      - из видимого текста (soup.get_text())
      - из ссылок mailto:
      - из сырого HTML (страховка от e-mail в атрибутах/скриптах)
    """
    emails = set()

    # 1) Видимый текст
    for match in EMAIL_REGEX.findall(soup.get_text(" ")):
        emails.add(match.strip().strip('.').lower())

    # 2) mailto:
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if href.lower().startswith('mailto:'):
            addr = href[len('mailto:'):].split('?')[0].strip()
            if addr and EMAIL_REGEX.fullmatch(addr):
                emails.add(addr.lower())

    # 3) Сырой HTML (на случай e-mail внутри <script>, <meta> и т.п.)
    for match in EMAIL_REGEX.findall(html_text):
        emails.add(match.strip().strip('.').lower())

    # Чистим мусор: некоторые регулярки могут зацепить куски JS/CSS
    cleaned = set()
    for e in emails:
        if e.count('@') != 1:
            continue
        local, domain = e.split('@', 1)
        if '.' not in domain or len(domain) < 4:
            continue
        if domain.endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg',
                            '.css', '.js')):
            continue
        cleaned.add(e)
    return cleaned


def extract_links(soup: BeautifulSoup, base_url: str) -> set:
    """Собирает абсолютные http(s) ссылки со страницы."""
    links = set()
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href:
            continue
        low = href.lower()
        if low.startswith(('javascript:', 'mailto:', 'tel:', 'data:', '#')):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme in ('http', 'https'):
            links.add(normalize_url(absolute))
    return links


# ---------------------------------------------------------------------------
# Обход
# ---------------------------------------------------------------------------

def crawl(start_url: str, max_depth: int, stay_in_domain: bool,
          include_subdomains: bool, skip_extensions: set):
    """
    Обходит сайт в ширину.
    Возвращает:
        email_data: { email -> { page_url -> count } }
        visited:    множество обработанных URL
        skipped:    сколько URL пропущено из-за skip_extensions
    """
    base_host = get_host(start_url)
    visited = set()
    email_data = defaultdict(lambda: defaultdict(int))
    skipped_count = 0

    session = requests.Session()
    session.headers.update(HEADERS)
    session.trust_env = False  # игнорируем системный прокси

    queue = [(normalize_url(start_url), 0)]

    while queue:
        url, depth = queue.pop(0)

        if url in visited:
            continue
        if stay_in_domain and not same_domain(url, base_host,
                                              include_subdomains):
            continue

        # Быстрая проверка расширения по URL (не делаем даже запрос)
        if url_has_skipped_extension(url, skip_extensions):
            print(f"[глубина {depth}] {url}  →  пропуск (расширение)")
            visited.add(url)
            skipped_count += 1
            continue

        visited.add(url)
        print(f"[глубина {depth}] {url}")

        try:
            # stream=True — тело не читается, пока не решим, что это HTML
            resp = session.get(url, timeout=10, stream=True)
        except requests.RequestException as e:
            print(f"    ! Ошибка запроса: {e}")
            time.sleep(REQUEST_DELAY)
            continue

        ctype = resp.headers.get('Content-Type', '')

        # Проверка по Content-Type (для URL без расширения, например /download?id=5)
        if response_is_skipped(ctype, skip_extensions):
            print(f"    → пропуск (Content-Type: {ctype or 'неизвестен'})")
            resp.close()
            skipped_count += 1
            time.sleep(REQUEST_DELAY)
            continue

        # Читаем тело только для HTML
        try:
            html_text = resp.text
        except Exception:
            html_text = resp.content.decode('utf-8', errors='ignore')
        finally:
            resp.close()

        soup = BeautifulSoup(html_text, 'html.parser')

        # --- Шаг 3: e-mail адреса ---
        page_emails = extract_emails(soup, html_text)
        if page_emails:
            print(f"    Найдено e-mail: {len(page_emails)}")
            for em in page_emails:
                email_data[em][url] += 1

        # --- Шаг 4: ссылки и рекурсия ---
        if depth < max_depth:
            new_links = extract_links(soup, url)
            for link in new_links:
                if link in visited:
                    continue
                if stay_in_domain and not same_domain(link, base_host,
                                                      include_subdomains):
                    continue
                # Заранее отсеиваем ссылки с ненужными расширениями,
                # чтобы не тащить их даже в очередь
                if url_has_skipped_extension(link, skip_extensions):
                    continue
                queue.append((link, depth + 1))

        time.sleep(REQUEST_DELAY)

    return email_data, visited, skipped_count


# ---------------------------------------------------------------------------
# Отчёт
# ---------------------------------------------------------------------------

def write_report(report_file: str, email_data, visited_count: int,
                 skipped_count: int, start_url: str, max_depth: int,
                 stay_in_domain: bool, include_subdomains: bool,
                 skip_extensions: set):
    """Формирует текстовый отчёт."""
    def total_count(item):
        return sum(item[1].values())

    sorted_emails = sorted(email_data.items(),
                           key=lambda kv: (-total_count(kv), kv[0]))

    with open(report_file, 'w', encoding='utf-8') as f:
        f.write("=" * 80 + "\n")
        f.write(" ОТЧЁТ ПО СБОРУ E-MAIL АДРЕСОВ\n")
        f.write("=" * 80 + "\n\n")
        f.write(f"Дата запуска:         "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Стартовый URL:        {start_url}\n")
        f.write(f"Глубина обхода:       {max_depth}\n")
        f.write(f"Оставаться в домене:  "
                f"{'да' if stay_in_domain else 'нет'}\n")
        if stay_in_domain:
            f.write(f"Учитывать поддомены:  "
                    f"{'да' if include_subdomains else 'нет'}\n")
        f.write(f"Обработано страниц:   {visited_count}\n")
        f.write(f"Пропущено файлов:     {skipped_count}\n")
        f.write(f"Уникальных e-mail:    {len(email_data)}\n")
        if skip_extensions:
            f.write(f"Фильтр расширений:    "
                    f"{', '.join(sorted(skip_extensions))}\n")
        else:
            f.write("Фильтр расширений:    не задан\n")
        f.write("\n" + "=" * 80 + "\n\n")

        if not sorted_emails:
            f.write("E-mail адреса не найдены.\n")
            return

        for idx, (email, pages) in enumerate(sorted_emails, 1):
            total = sum(pages.values())
            f.write(f"[{idx}] {email}\n")
            f.write(f"    Всего вхождений: {total}\n")
            f.write(f"    Страниц с этим адресом: {len(pages)}\n")
            f.write( "    Перечень страниц:\n")
            for page_url in sorted(pages):
                cnt = pages[page_url]
                if cnt > 1:
                    f.write(f"      - {page_url}  (вхождений: {cnt})\n")
                else:
                    f.write(f"      - {page_url}\n")
            f.write("\n")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else 'config.ini'

    (url, depth, stay_in_domain, include_subdomains,
     report_base, skip_extensions) = read_config(config_path)

    report_file = build_report_filename(report_base)

    print("=" * 60)
    print(f"Стартовый URL:       {url}")
    print(f"Глубина обхода:      {depth}")
    print(f"Оставаться в домене: {'да' if stay_in_domain else 'нет'}")
    if stay_in_domain:
        print(f"Учитывать поддомены: "
              f"{'да' if include_subdomains else 'нет'}")
    if skip_extensions:
        print(f"Пропускаем расширения: "
              f"{', '.join(sorted(skip_extensions))}")
    else:
        print("Фильтр расширений:   не задан")
    print(f"Файл отчёта:         {report_file}")
    print("=" * 60)
    print()

    email_data, visited, skipped = crawl(
        url, depth, stay_in_domain, include_subdomains, skip_extensions
    )

    write_report(report_file, email_data, len(visited), skipped,
                 url, depth, stay_in_domain, include_subdomains,
                 skip_extensions)

    print()
    print("=" * 60)
    print(f"✅ Обход завершён. Обработано страниц: {len(visited)}")
    print(f"⏭  Пропущено файлов по фильтру: {skipped}")
    print(f"📧 Найдено уникальных e-mail: {len(email_data)}")
    print(f"📄 Отчёт записан в файл: {report_file}")


if __name__ == '__main__':
    main()