#!/usr/bin/env python3
"""
Actualiza automáticamente las tablas de posiciones del sitio de Santa Elena
Universitario a partir de las fuentes públicas de cada liga.

Cómo funciona:
  - Por cada tabla, hay una función scrape_<liga>() que baja la fuente y
    devuelve una lista de filas: [{"equipo": str, "j": int, "g": int,
    "e": int, "p": int, "gf": int, "gc": int}, ...] ya ordenadas por
    posición (o con "pts" incluido si la fuente no permite recalcularlo).
  - build_rows_html() arma el <tbody> exacto con el mismo formato que ya
    usa index.html (clases pos/tabnum, fila "us" resaltada para Santa Elena).
  - update_block() reemplaza el contenido entre los comentarios
    "<!-- AUTO-TABLE:<nombre>:START -->" y "...:END -->" dentro de index.html.
  - main() corre todas las ligas disponibles y guarda index.html solo si
    algo cambió (para que el commit en GitHub Actions sea prolijo).

Fase 1 (esta versión): solo ACB (handball) está implementado, porque es la
única fuente que se sirve como HTML plano y se puede scrapear con
requests + BeautifulSoup sin un navegador. LUD y Montevideo Girls Cup son
aplicaciones JavaScript (SPA) — sus scrapers (scrape_lud, scrape_mgc) están
en TODO y se agregan en la fase 2, con Playwright.
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


def scrape_acb(url: str):
    """Handball - ACB (Liga Mayor B). Tabla en HTML plano."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    target_table = None
    for table in soup.find_all("table"):
        header_cells = [
            th.get_text(strip=True).lower()
            for th in table.find_all(["th", "td"])[:10]
        ]
        header_text = " ".join(header_cells)
        if "equipo" in header_text and "pts" in header_text:
            target_table = table
            break

    if target_table is None:
        raise RuntimeError("No encontré la tabla de posiciones en la página de ACB (cambió el formato).")

    rows = []
    body_rows = target_table.find_all("tr")
    for tr in body_rows:
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 9:
            continue  # fila de encabezado u otra cosa que no es una fila de equipo
        # Formato esperado: Pos, Equipo, J, G, E, P, GF, GC, Dif, Pts
        try:
            pos, equipo, j, g, e, p, gf, gc, dif, pts = cells[:10]
            int(j)  # valida que efectivamente sean números
        except ValueError:
            continue
        rows.append({
            "equipo": equipo,
            "j": j, "g": g, "e": e, "p": p,
            "gf": gf, "gc": gc, "dif": dif, "pts": pts,
        })

    if not rows:
        raise RuntimeError("La tabla de ACB se encontró pero no pude leer ninguna fila de datos.")

    return rows


def scrape_lud(phase_url: str):
    """TODO fase 2: LUD (lud-stats.vercel.app) es una SPA de React/Next.js,
    necesita un navegador headless (Playwright) para renderizar antes de
    poder leer la tabla. Se agrega en el próximo paso."""
    raise NotImplementedError("LUD todavía no está implementado (fase 2, requiere Playwright).")


def scrape_mgc(url: str):
    """TODO fase 2: Montevideo Girls Cup también es una SPA JavaScript,
    misma solución (Playwright) que LUD."""
    raise NotImplementedError("Montevideo Girls Cup todavía no está implementado (fase 2, requiere Playwright).")


def build_rows_html(rows) -> str:
    lines = []
    for i, r in enumerate(rows, start=1):
        row_class = ' class="us"' if _is_our_team(r["equipo"]) else ""
        lines.append(
            f'              <tr{row_class}><td class="pos tabnum">{i}</td>'
            f'<td>{r["equipo"]}</td>'
            f'<td class="tabnum">{r["j"]}</td>'
            f'<td class="tabnum">{r["g"]}</td>'
            f'<td class="tabnum">{r["e"]}</td>'
            f'<td class="tabnum">{r["p"]}</td>'
            f'<td class="tabnum">{r["gf"]}</td>'
            f'<td class="tabnum">{r["gc"]}</td>'
            f'<td class="tabnum">{r["dif"]}</td>'
            f'<td class="tabnum">{r["pts"]}</td></tr>'
        )
    return "\n".join(lines)


def update_block(html: str, block_name: str, rows_html: str) -> str:
    start_marker = f"<!-- AUTO-TABLE:{block_name}:START -->"
    end_marker = f"<!-- AUTO-TABLE:{block_name}:END -->"
    pattern = re.compile(
        re.escape(start_marker) + r".*?" + re.escape(end_marker),
        re.DOTALL,
    )
    if not pattern.search(html):
        raise RuntimeError(
            f"No encontré los marcadores {start_marker} / {end_marker} en index.html "
            "(¿se editó el archivo y se borraron los comentarios?)."
        )
    replacement = f"{start_marker}\n{rows_html}\n{end_marker}"
    return pattern.sub(replacement, html, count=1)


SOURCES = {
    "handball": {
        "fn": scrape_acb,
        "url": "https://www.acb.com.uy/web/liga-mayor-b-2026/",
    },
    # Se agregan en fase 2:
    # "mayores": {"fn": scrape_lud, "url": "https://lud-stats.vercel.app/?phase=3"},
    # "reserva": {"fn": scrape_lud, "url": "https://lud-stats.vercel.app/?phase=9"},
    # "sub20":   {"fn": scrape_lud, "url": "https://lud-stats.vercel.app/?phase=24"},
    # "sub18":   {"fn": scrape_lud, "url": "https://lud-stats.vercel.app/?phase=28"},
    # "sub16":   {"fn": scrape_lud, "url": "https://lud-stats.vercel.app/?phase=34"},
    # "femenino": {"fn": scrape_mgc, "url": "https://montevideogirlscup.uy/ranking"},
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
            rows = cfg["fn"](cfg["url"])
            rows_html = build_rows_html(rows)
            html = update_block(html, block_name, rows_html)
            print(f"[ok] {block_name}: {len(rows)} equipos actualizados")
        except NotImplementedError as e:
            print(f"[skip] {block_name}: {e}")
        except Exception as e:
            # Si una fuente falla (la página cambió, está caída, etc.) no
            # tocamos esa tabla y seguimos con las demás, pero marcamos
            # error para que el Action avise.
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
