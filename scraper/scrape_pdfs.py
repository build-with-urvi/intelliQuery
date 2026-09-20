"""
One-time helper tool: NOT part of the core RAG pipeline.

Given a university page URL (e.g. the Admission Brochures page, the
Syllabus page, the Notices page), this script finds every PDF link on
that page and downloads it into your documents/ folder automatically,
instead of you clicking "Save link as" one by one.

Run from the scraper/ folder:
    python scrape_pdfs.py "https://ipu.ac.in/adm2026/adm2026brochures.php"

Optional flags:
    --limit 10           only download the first 10 links found
    --years 2025,2026     only download links mentioning these years

You can run it again with a different URL for each listing page
(admission brochures, syllabus, notices, etc.) - it will keep adding
new PDFs to documents/ without deleting what's already there.
"""
import os
import time
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

DOCUMENTS_FOLDER = "../documents"
DELAY_BETWEEN_DOWNLOADS = 1.5  # seconds - be polite, don't hammer the server
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; student-project-bot/1.0)"
}


def get_pdf_links(page_url: str) -> list[str]:
    response = requests.get(page_url, headers=HEADERS, timeout=15)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    pdf_links = []

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if href.lower().endswith(".pdf"):
            full_url = urljoin(page_url, href)
            pdf_links.append(full_url)

    return list(dict.fromkeys(pdf_links))


def safe_filename_from_url(url: str) -> str:
    path = urlparse(url).path
    filename = os.path.basename(path)
    if not filename:
        filename = f"document_{abs(hash(url)) % 100000}.pdf"
    return filename


def download_pdf(url: str, folder: str) -> str | None:
    filename = safe_filename_from_url(url)
    filepath = os.path.join(folder, filename)

    if os.path.exists(filepath):
        print(f"  Skipping (already downloaded): {filename}")
        return filepath

    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()

        if not response.content.startswith(b"%PDF"):
            print(f"  Skipped (not a real PDF): {url}")
            return None

        with open(filepath, "wb") as f:
            f.write(response.content)

        print(f"  Downloaded: {filename} ({len(response.content) // 1024} KB)")
        return filepath

    except requests.exceptions.RequestException as e:
        print(f"  Failed: {url} -> {e}")
        return None


def filter_by_year(pdf_links: list[str], years: list[str]) -> list[str]:
    filtered = []
    for url in pdf_links:
        if any(year in url for year in years):
            filtered.append(url)
    return filtered


def scrape_page(page_url: str, limit: int = None, years: list[str] = None):
    os.makedirs(DOCUMENTS_FOLDER, exist_ok=True)

    print(f"Fetching page: {page_url}")
    pdf_links = get_pdf_links(page_url)
    print(f"Found {len(pdf_links)} PDF link(s) on this page.")

    if years:
        before = len(pdf_links)
        pdf_links = filter_by_year(pdf_links, years)
        print(f"Filtered to {len(pdf_links)} link(s) matching year(s) {years} (from {before}).")

    if limit:
        pdf_links = pdf_links[:limit]
        print(f"Limiting to the first {limit} link(s) (assumes newest-first page ordering).")

    print()

    if not pdf_links:
        print("No PDFs found. The page may load links via JavaScript, "
              "or PDFs may be linked from a sub-page instead.")
        return

    downloaded = 0
    for i, url in enumerate(pdf_links, 1):
        print(f"[{i}/{len(pdf_links)}] {url}")
        result = download_pdf(url, DOCUMENTS_FOLDER)
        if result:
            downloaded += 1
        time.sleep(DELAY_BETWEEN_DOWNLOADS)

    print(f"\nDone. {downloaded}/{len(pdf_links)} PDFs saved to {DOCUMENTS_FOLDER}/")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Download PDFs linked from a university page.")
    parser.add_argument("url", help="Page URL to scrape for PDF links")
    parser.add_argument("--limit", type=int, default=None,
                         help="Only download the first N links found")
    parser.add_argument("--years", type=str, default=None,
                         help="Comma-separated years to keep, e.g. --years 2025,2026")
    args = parser.parse_args()

    year_list = args.years.split(",") if args.years else None
    scrape_page(args.url, limit=args.limit, years=year_list)
