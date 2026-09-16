#!/usr/bin/env python3
"""
Actualiza automáticamente las tablas de posiciones del sitio de Santa Elena
Universitario a partir de las fuentes públicas de cada liga.

Cómo funciona:
  - Por cada tabla, hay una función scrape_<liga>() que baja la fuente y
    devuelve una lista de filas ya ordenadas por posición.
  - build_rows_html_10() / build_rows_html_5() arman el <tbody> exacto con
    el mismo formato que ya usa index.html (clases pos/tabnum, fila "us"
    resaltada para Santa Elena).
  - update_block() reemplaza el contenido entre los comentarios
    "<!-- AUTO-TABLE:<nombre>:START -->" y "...:END -->" dentro de index.html.
  - main() corre todas las ligas disponibles y guarda index.html solo si
    algo cambió (para que el commit en GitHub Actions sea prolijo). Si una
    fuente falla, no toca esa tabla y sigue con las demás.

Fuentes:
  - ACB (handball): HTML plano, se lee con requests + BeautifulSoup.
  - LUD (mayores/reserva/sub20/sub18/sub16): SPA de React, necesita
    Playwright para renderizar antes de leer la tabla.
  - Montevideo Girls Cup (femenino): también SPA. Además hay que clickear
    el filtro de categoría (ej. "D") y la pestaña "POSICIONES" antes de
    leer la tabla, porque el sitio no usa un link directo por categoría.
"""
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / "index.html"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SantaElenaBot/1.0; +https://santaelenau.com)"
}

OUR_TEAM_MARKERS = ["santa elena"]


def _is_our_team(name: str) -> bool:
    n = name.strip().lower()
    return any(marker in n for marker in OUR_TEAM_MARKERS)


def _num(text: str) -> str:
    """Limpia un texto tipo '🏆 1' o '+16' y devuelve solo el número (con signo si tiene)."""
    m = re.search(r"-?\+?\d+", text.replace(" ", ""))
    return m.group(0).lstrip("+") if m else text.strip()


# ---------------------------------------------------------------------------
# ACB (handball) - HTML plano
# ---------------------------------------------------------------------------

def scrape_acb(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    target_table = None
    for table in soup.find_all("table"):
        header_cells = [th.get_text(strip=True).lower() for th in table.find_all(["th", "td"])[:10]]
        header_text = " ".join(header_cells)
        if "equipo" in header_text and "pts" in header_text:
            target_table = table
            break

    if target_table is None:
        raise RuntimeError("No encontré la tabla de posiciones en la página de ACB (cambió el formato).")

    rows = []
    for tr in target_table.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 9:
            continue
        try:
            pos, equipo, j, g, e, p, gf, gc, dif, pts = cells[:10]
            int(j)
        except ValueError:
            continue
        rows.append({"equipo": equipo, "j": j, "g": g, "e": e, "p": p, "gf": gf, "gc": gc, "dif": dif, "pts": pts})

    if not rows:
        raise RuntimeError("La tabla de ACB se encontró pero no pude leer ninguna fila de datos.")
    return rows


# ---------------------------------------------------------------------------
# LUD (mayores/reserva/sub20/sub18/sub16) - SPA de React (Next.js), necesita
# navegador headless para renderizar.
# ---------------------------------------------------------------------------

def _get_playwright_page(browser, url: str, wait_selector: str = "table"):
    page = browser.new_page()
    page.goto(url, wait_until="networkidle", timeout=45000)
    try:
        page.wait_for_selector(wait_selector, timeout=15000)
    except Exception:
        pass  # seguimos igual, puede que la tabla ya esté aunque el selector tarde
    page.wait_for_timeout(1000)
    return page


def _extract_table_rows(page, min_cols: int = 5):
    """Devuelve la tabla (lista de filas, cada una lista de celdas de texto)
    con más filas de todas las que haya en la página — asumimos que es la
    tabla de posiciones principal."""
    tables = page.eval_on_selector_all(
        "table",
        """
        tables => tables.map(t =>
          Array.from(t.querySelectorAll('tr')).map(tr =>
            Array.from(tr.querySelectorAll('td,th')).map(c => c.innerText.trim())
          )
        )
        """,
    )
    candidates = [t for t in tables if len(t) > 1 and len(t[-1]) >= min_cols]
    if not candidates:
        raise RuntimeError("No encontré ninguna tabla con datos en la página (después de renderizar con JS).")
    return max(candidates, key=len)


def scrape_lud(url: str):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = _get_playwright_page(browser, url, wait_selector="table")
            raw_rows = _extract_table_rows(page, min_cols=9)
        finally:
            browser.close()

    rows = []
    for cells in raw_rows:
        if len(cells) < 9:
            continue
        # esperamos: Pos, Equipo, PJ, G, E, P, GF, GC, DG, Pts (10 cols) —
        # si el sitio agrega/saca una columna, esto puede desalinearse y
        # el chequeo de más abajo (que "j" sea numérico) hace que la fila
        # se descarte en vez de guardar basura.
        try:
            equipo = cells[-9]
            j, g, e, p, gf, gc, dif, pts = cells[-8:]
            int(_num(j))
        except (ValueError, IndexError):
            continue
        rows.append({
            "equipo": equipo.strip(),
            "j": _num(j), "g": _num(g), "e": _num(e), "p": _num(p),
            "gf": _num(gf), "gc": _num(gc), "dif": _num(dif), "pts": _num(pts),
        })

    if not rows:
        raise RuntimeError("La tabla de LUD se encontró pero no pude leer ninguna fila de datos (revisar formato).")
    return rows


# ---------------------------------------------------------------------------
# Montevideo Girls Cup (femenino) - SPA, hay que clickear categoría "D" y
# la pestaña "Posiciones" antes de leer la tabla.
# ---------------------------------------------------------------------------

def scrape_mgc(url: str, categoria: str = "D"):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(1500)

            # Filtro de categoría (botón con el texto exacto, ej "D")
            try:
                page.get_by_text(categoria, exact=True).first.click(timeout=8000)
            except Exception:
                pass  # si no lo encuentra, seguimos con lo que haya cargado por defecto

            # Pestaña "Posiciones" (puede que ya esté activa por defecto)
            try:
                page.get_by_text(re.compile("posiciones", re.I)).first.click(timeout=8000)
            except Exception:
                pass

            page.wait_for_timeout(1500)
            raw_rows = _extract_table_rows(page, min_cols=4)
        finally:
            browser.close()

    rows = []
    for cells in raw_rows:
        if len(cells) < 4:
            continue
        # esperamos: #, Equipo, PJ, +/-, Pts (5 cols, la primera puede traer
        # un ícono de trofeo pegado al número en el top 3)
        try:
            equipo = cells[-4]
            pj, dif, pts = cells[-3:]
            int(_num(pj))
        except (ValueError, IndexError):
            continue
        rows.append({"equipo": equipo.strip(), "pj": _num(pj), "dif": dif.strip(), "pts": _num(pts)})

    if not rows:
        raise RuntimeError("La tabla de Montevideo Girls Cup se encontró pero no pude leer ninguna fila de datos.")
    return rows


# ---------------------------------------------------------------------------
# Armado del HTML y reemplazo en index.html
# ---------------------------------------------------------------------------

def build_rows_html_10(rows) -> str:
    lines = []
    for i, r in enumerate(rows, start=1):
        row_class = ' class="us"' if _is_our_team(r["equipo"]) else ""
        lines.append(
            f'              <tr{row_class}><td class="pos tabnum">{i}</td>'
            f'<td>{r["equipo"]}</td>'
            f'<td class="tabnum">{r["j"]}</td><td class="tabnum">{r["g"]}</td>'
            f'<td class="tabnum">{r["e"]}</td><td class="tabnum">{r["p"]}</td>'
            f'<td class="tabnum">{r["gf"]}</td><td class="tabnum">{r["gc"]}</td>'
            f'<td class="tabnum">{r["dif"]}</td><td class="tabnum">{r["pts"]}</td></tr>'
        )
    return "\n".join(lines)


def build_rows_html_5(rows) -> str:
    lines = []
    for i, r in enumerate(rows, start=1):
        row_class = ' class="us"' if _is_our_team(r["equipo"]) else ""
        lines.append(
            f'              <tr{row_class}><td class="pos tabnum">{i}</td>'
            f'<td>{r["equipo"]}</td>'
            f'<td class="tabnum">{r["pj"]}</td>'
            f'<td class="tabnum">{r["dif"]}</td>'
            f'<td class="tabnum">{r["pts"]}</td></tr>'
        )
    return "\n".join(lines)


def update_block(html: str, block_name: str, rows_html: str) -> str:
    start_marker = f"<!-- AUTO-TABLE:{block_name}:START -->"
    end_marker = f"<!-- AUTO-TABLE:{block_name}:END -->"
    pattern = re.compile(re.escape(start_marker) + r".*?" + re.escape(end_marker), re.DOTALL)
    if not pattern.search(html):
        raise RuntimeError(
            f"No encontré los marcadores {start_marker} / {end_marker} en index.html."
        )
    replacement = f"{start_marker}\n{rows_html}\n{end_marker}"
    return pattern.sub(replacement, html, count=1)


SOURCES = {
    "handball": {"fn": scrape_acb, "args": ("https://www.acb.com.uy/web/liga-mayor-b-2026/",), "build": build_rows_html_10},
    "mayores": {"fn": scrape_lud, "args": ("https://lud-stats.vercel.app/?phase=3",), "build": build_rows_html_10},
    "reserva": {"fn": scrape_lud, "args": ("https://lud-stats.vercel.app/?phase=9",), "build": build_rows_html_10},
    "sub20": {"fn": scrape_lud, "args": ("https://lud-stats.vercel.app/?phase=24",), "build": build_rows_html_10},
    "sub18": {"fn": scrape_lud, "args": ("https://lud-stats.vercel.app/?phase=28",), "build": build_rows_html_10},
    "sub16": {"fn": scrape_lud, "args": ("https://lud-stats.vercel.app/?phase=34",), "build": build_rows_html_10},
    "femenino": {"fn": scrape_mgc, "args": ("https://montevideogirlscup.uy/ranking", "D"), "build": build_rows_html_5},
}


def main():
    if not INDEX_HTML.exists():
        print(f"No encuentro {INDEX_HTML}", file=sys.stderr)
        sys.exit(1)

    html = INDEX_HTML.read_text(encoding="utf-8")
    original_html = html
    any_error = False

    for block_name, cfg in SOURCES.items():
        try:
            rows = cfg["fn"](*cfg["args"])
            rows_html = cfg["build"](rows)
            html = update_block(html, block_name, rows_html)
            print(f"[ok] {block_name}: {len(rows)} equipos actualizados")
        except Exception as e:
            print(f"[error] {block_name}: {e}", file=sys.stderr)
            any_error = True

    if html != original_html:
        INDEX_HTML.write_text(html, encoding="utf-8")
        print("index.html actualizado.")
    else:
        print("Sin cambios en index.html.")

    if any_error:
        sys.exit(2)


if __name__ == "__main__":
    main()
