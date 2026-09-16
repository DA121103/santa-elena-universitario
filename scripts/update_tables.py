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
# "Santa Elena Lagomar" es OTRO club (comparte el nombre "Santa Elena" de
# pura coincidencia) - hay que excluirlo explícitamente para no confundirlo
# con el nuestro (Colegio Santa Elena / Santa Elena U / Santa Elena
# Universitario).
OUR_TEAM_EXCLUDE = ["lagomar"]


def _is_our_team(name: str) -> bool:
    n = name.strip().lower()
    if any(x in n for x in OUR_TEAM_EXCLUDE):
        return False
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
        # En vez de asumir posiciones fijas (Pos, Equipo, PJ...), que se
        # desalinean si el sitio agrega una celda vacía (ej. el escudo del
        # equipo sin texto), buscamos el nombre del equipo por CONTENIDO:
        # es la primera celda que tiene letras. Los 8 números que vienen
        # después son J, G, E, P, GF, GC, DG, Pts, ignorando cualquier
        # celda vacía o no-numérica que se cuele en el medio.
        equipo_idx = next((i for i, c in enumerate(cells) if re.search(r"[A-Za-zÁÉÍÓÚáéíóúÑñ]", c)), None)
        if equipo_idx is None:
            continue
        equipo = cells[equipo_idx].strip()
        # tomamos todas las celdas numéricas de la fila (antes Y después
        # del nombre, por si el sitio ordena las columnas distinto a lo
        # esperado), salvo la primera celda de la fila que asumimos que es
        # el número de posición/ranking y no una estadística.
        nums = [
            c for i, c in enumerate(cells)
            if i != 0 and i != equipo_idx and re.fullmatch(r"-?\+?\d+", c.replace(" ", ""))
        ]
        if len(nums) < 8:
            continue
        j, g, e, p, gf, gc, dif, pts = nums[:8]
        rows.append({
            "equipo": equipo,
            "j": _num(j), "g": _num(g), "e": _num(e), "p": _num(p),
            "gf": _num(gf), "gc": _num(gc), "dif": _num(dif), "pts": _num(pts),
        })

    if not rows:
        raise RuntimeError("La tabla de LUD se encontró pero no pude leer ninguna fila de datos (revisar formato).")
    if not any(_is_our_team(r["equipo"]) for r in rows):
        raise RuntimeError(
            f"Leí una tabla de {len(rows)} equipos pero 'Santa Elena' no está en ninguno — "
            f"probablemente agarré la tabla equivocada de la página. No guardo esto. "
            f"Equipos leídos: {', '.join(r['equipo'] for r in rows[:5])}..."
        )
    return rows


# ---------------------------------------------------------------------------
# Montevideo Girls Cup (femenino) - SPA, hay que clickear categoría "D" y
# la pestaña "Posiciones" antes de leer la tabla.
# ---------------------------------------------------------------------------

def _parse_rows_from_text(lines, n_numeric_cols: int, header_word: str):
    """Método a prueba de balas para tablas armadas con <div>/CSS grid (sin
    <table> real), típico en apps de React: en vez de navegar el DOM, toma
    el texto VISIBLE de la página línea por línea y busca el patrón
    "posición (número chico) -> nombre de equipo (texto) -> N números
    seguidos" repetido. No le importa qué etiqueta HTML se usó, solo el
    orden en que se lee la pantalla.
    """
    # arrancamos después de la última vez que aparece la palabra de
    # encabezado (ej. "PTS"), para no confundir el encabezado con datos
    start = 0
    for i, l in enumerate(lines):
        if l.strip().upper() == header_word.upper():
            start = i + 1
    data = [l.strip() for l in lines[start:] if l.strip()]

    def is_pos(s):
        return bool(re.fullmatch(r"[🏆🥇🥈🥉#]*\s*\d{1,3}", s))

    def is_num(s):
        return bool(re.fullmatch(r"[+-]?\d{1,4}", s.replace(" ", "")))

    rows = []
    i = 0
    while i < len(data):
        if is_pos(data[i]) and i + 1 + n_numeric_cols < len(data) + 1:
            equipo = data[i + 1] if i + 1 < len(data) else None
            nums = data[i + 2: i + 2 + n_numeric_cols]
            if (
                equipo and not is_pos(equipo) and not is_num(equipo)
                and len(nums) == n_numeric_cols and all(is_num(n) for n in nums)
            ):
                rows.append([equipo] + nums)
                i += 2 + n_numeric_cols
                continue
        i += 1
    return rows


def scrape_mgc(url: str, categoria: str = "D"):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(1500)

            # Filtro de categoría (botón con el texto exacto, ej "D"). En vez
            # de clickear el primer elemento que diga "D" en toda la página
            # (puede haber varios y agarrar el equivocado), primero
            # identificamos el grupo real de botones de categoría: el
            # conjunto de elementos hermanos entre sí cuyo texto es
            # exactamente una de las categorías conocidas (A, B, C, D...),
            # y clickeamos "D" solo dentro de ese grupo.
            clicked = page.evaluate(
                """
                (categoria) => {
                  const knownCats = ['A','B','C','D','E','F','G','+30','Jueves','Sub15'];
                  const all = Array.from(document.querySelectorAll('body *'));
                  const candidates = all.filter(el =>
                    el.children.length === 0 && knownCats.includes(el.textContent.trim())
                  );
                  const byParent = new Map();
                  candidates.forEach(el => {
                    const p = el.parentElement;
                    if (!p) return;
                    if (!byParent.has(p)) byParent.set(p, []);
                    byParent.get(p).push(el);
                  });
                  let bestParent = null, bestCount = 0;
                  for (const [p, els] of byParent.entries()) {
                    if (els.length > bestCount) { bestCount = els.length; bestParent = p; }
                  }
                  if (!bestParent || bestCount < 3) return false;
                  const target = byParent.get(bestParent).find(el => el.textContent.trim() === categoria);
                  if (target) { target.click(); return true; }
                  return false;
                }
                """,
                categoria,
            )
            if not clicked:
                print(f"[warn] no pude ubicar con certeza el botón de categoría '{categoria}'", file=sys.stderr)

            # Pestaña "Posiciones" (puede que ya esté activa por defecto)
            try:
                page.get_by_text(re.compile("posiciones", re.I)).first.click(timeout=8000)
            except Exception:
                pass

            page.wait_for_timeout(1500)
            body_text = page.inner_text("body")
        finally:
            browser.close()

    lines = body_text.split("\n")
    parsed = _parse_rows_from_text(lines, n_numeric_cols=3, header_word="PTS")
    if not parsed:
        raise RuntimeError(
            "Encontré la página pero no pude reconocer el patrón de la tabla en el texto "
            "(#, equipo, PJ, +/-, PTS). Puede que cambiara el formato del sitio."
        )

    rows = []
    for equipo, pj, dif, pts in parsed:
        rows.append({"equipo": equipo.strip(), "pj": _num(pj), "dif": dif.strip(), "pts": _num(pts)})

    if not rows:
        raise RuntimeError("La tabla de Montevideo Girls Cup se encontró pero no pude leer ninguna fila de datos.")
    if not any(_is_our_team(r["equipo"]) for r in rows):
        raise RuntimeError(
            f"Leí una tabla de {len(rows)} equipos pero 'Santa Elena' no está en ninguno — "
            f"probablemente el filtro de categoría '{categoria}' clickeó mal. No guardo esto "
            f"para no pisar la tabla con datos de otra divisional. Equipos leídos: "
            f"{', '.join(r['equipo'] for r in rows[:5])}..."
        )
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
