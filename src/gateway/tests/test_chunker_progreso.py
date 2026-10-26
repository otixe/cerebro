"""Property test de PROGRESO y NO-PERDIDA de _split_large (tarea b0c14345, 2026-08-04).

Que prueba
----------
El bug F1: `i = max(cut - overlap, i + 1)` garantizaba TERMINACION pero no PROGRESO.
Con un separador a menos de `overlap` chars del cursor, el cursor avanzaba 1 char por
iteracion y el mismo texto se re-emitia cientos de veces desplazado 1 (35.080 fragmentos
donde correspondian 2.958 en el indice real).

Y prueba, sobre todo, que el FIX no introduce el bug INVERSO: un salto
`i = max(cut - overlap, i + step)` termina y no infla, pero PIERDE el texto entre `cut`
y el cursor nuevo (medido: 69.833 chars en 43/55 docs reales). Por eso el assert (a)
—cobertura de caracteres del original— es el mas importante junto con (e).

Como correrlo
-------------
    # contra el codigo vivo (gateway/chunker.py, default):
    python3 gateway/tests/test_chunker_progreso.py

    # contra CUALQUIER otra version (control: debe FALLAR con el codigo pre-fix):
    CHUNKER_PATH=/ruta/a/chunker_old.py python3 gateway/tests/test_chunker_progreso.py

    # tambien corre con unittest/pytest si estan disponibles:
    python3 -m unittest discover -s gateway/tests -v

Exit code 0 = PASS. Un test que no falla con el codigo viejo no prueba nada: los asserts
(c) shift-1 y (d) patologico<=3 son los discriminantes que DEBEN fallar contra el viejo.

Metodologia del assert (a)
--------------------------
La cobertura NO se mide comparando fragmentos entre si (eso no ve la perdida: el texto
que sobrevive sigue siendo valido). Se mide instrumentando los SPANS (i, cut) reales y
pintando un bitmap de los caracteres del original. El instrumentador no puede mentir:
prueba las politicas conocidas de guard/salto y exige que los fragmentos reconstruidos
sean EXACTAMENTE IGUALES a los que devuelve la implementacion bajo test; si ninguna
politica reproduce la salida, el test FALLA pidiendo actualizar el instrumentador en vez
de reportar un numero inventado.
"""
import importlib.util
import os
import random
import sys
import unittest

# --------------------------------------------------------------- carga del modulo
_HERE = os.path.dirname(os.path.abspath(__file__))
CHUNKER_PATH = os.environ.get(
    "CHUNKER_PATH", os.path.join(os.path.dirname(_HERE), "chunker.py")
)


def _load(path):
    spec = importlib.util.spec_from_file_location("chunker_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chunker = _load(CHUNKER_PATH)
MAX_CHARS = chunker.MAX_CHARS            # 1848
OVERLAP = chunker.OVERLAP_CHARS          # 400
MIN_PROGRESS = getattr(chunker, "MIN_PROGRESS_CHARS", chunker.MIN_CHARS)  # 200
STEP = MAX_CHARS - OVERLAP               # 1448
split = chunker._split_large


# ------------------------------------------------- referencia congelada del codigo viejo
def split_frozen_old(text, max_chars, overlap):
    """Copia LITERAL de _split_large previo al FIX F1 (md5 940290ea del chunker viejo).

    Congelada aqui a proposito: el assert (e) —anti-regresion— necesita saber que
    producia el codigo viejo en documentos SANOS, incluso cuando ese archivo ya no exista.
    NO usar como implementacion: tiene el bug de progreso.
    """
    if len(text) <= max_chars:
        return [text]
    out = []
    i = 0
    while i < len(text):
        end = min(i + max_chars, len(text))
        if end < len(text):
            cut = text.rfind("\n\n", i, end)
            if cut == -1:
                cut = text.rfind(". ", i, end)
            if cut == -1:
                cut = end
            out.append(text[i:cut])
            i = max(cut - overlap, i + 1)
        else:
            out.append(text[i:end])
            break
    return out


# ------------------------------------------------------------------- instrumentador
# Politicas conocidas: (nombre, guard(cut,i,end)->bool, jump(cut,i)->int).
# guard True => el corte se descarta y se usa el techo duro (cut = end).
_POLICIES = [
    ("OLD",          lambda c, i: False,
                     lambda c, i: max(c - OVERLAP, i + 1)),
    ("FIX_MINIMAL2", lambda c, i: c - i < OVERLAP + MIN_PROGRESS,
                     lambda c, i: c - OVERLAP),
    # variantes historicas, presentes solo para que el instrumentador las RECONOZCA
    # (y el test pueda medir su perdida) — no son implementaciones aceptables:
    ("FIX_SPEC",     lambda c, i: c <= i + OVERLAP,
                     lambda c, i: max(c - OVERLAP, i + STEP)),
    ("FIX_LURTZ",    lambda c, i: c - i < STEP,
                     lambda c, i: max(c - OVERLAP, i + STEP)),
    ("FIX_MINIMAL",  lambda c, i: c <= i + OVERLAP,
                     lambda c, i: c - OVERLAP),
]


def _spans_with(text, guard, jump, cap=200000):
    """Replica el bucle con una politica dada. Devuelve [(i, cut)] o None si no termina."""
    if len(text) <= MAX_CHARS:
        return [(0, len(text))]
    out = []
    i = 0
    guard_iters = 0
    while i < len(text):
        guard_iters += 1
        if guard_iters > cap:
            return None                      # no termina: politica descartada
        end = min(i + MAX_CHARS, len(text))
        if end < len(text):
            cut = text.rfind("\n\n", i, end)
            if cut == -1:
                cut = text.rfind(". ", i, end)
            if cut == -1:
                cut = end
            if guard(cut, i):
                cut = end
            out.append((i, cut))
            nxt = jump(cut, i)
            if nxt <= i:
                return None                  # politica invalida (no avanza)
            i = nxt
        else:
            out.append((i, end))
            break
    return out


def spans_validated(text, impl=None):
    """SPANS (i, cut) reales de `impl` sobre `text`, validados contra su salida.

    Devuelve (politicas_coincidentes, spans): TODAS las politicas conocidas cuyos
    fragmentos coinciden EXACTAMENTE con los de la implementacion bajo test. Puede
    haber empate legitimo (en documentos sanos OLD y FIX_MINIMAL2 producen lo mismo;
    de hecho eso ES el assert (e)); lo que no se permite es CERO coincidencias, ni
    que dos politicas coincidan en fragmentos pero difieran en spans (ahi la cobertura
    seria ambigua). El instrumentador nunca reporta un numero que no pudo reproducir.
    """
    impl = impl or split
    real = impl(text, MAX_CHARS, OVERLAP)
    matches, spans = [], None
    for name, guard, jump in _POLICIES:
        sp = _spans_with(text, guard, jump)
        if sp is None:
            continue
        if [text[a:b] for a, b in sp] == real:
            if spans is not None and sp != spans:
                raise AssertionError(
                    f"politicas {matches} y {name} dan los mismos fragmentos con spans "
                    "distintos: la cobertura seria ambigua")
            matches.append(name)
            spans = sp
    if not matches:
        raise AssertionError(
            "instrumentador desincronizado con la implementacion bajo test "
            f"({CHUNKER_PATH}): ninguna politica conocida reproduce sus {len(real)} "
            "fragmentos. Si cambiaste el algoritmo, agrega su politica a _POLICIES.")
    return tuple(matches), spans


def metrics(text, impl=None):
    names, sp = spans_validated(text, impl)
    # (a) cobertura de caracteres del original
    cov = bytearray(len(text))
    for a, b in sp:
        for k in range(a, min(b, len(text))):
            cov[k] = 1
    perdidos = [k for k, c in enumerate(cov) if not c]
    perdidos_reales = [k for k in perdidos if not text[k].isspace()]
    starts = [a for a, _ in sp]
    avances = [starts[k + 1] - starts[k] for k in range(len(starts) - 1)]
    lens = [b - a for a, b in sp]
    return {
        "policy": names,
        "n": len(sp),
        "perdidos": len(perdidos),
        "perdidos_reales": len(perdidos_reales),
        "min_avance": min(avances) if avances else len(text),
        "shift1": sum(1 for a in avances if a == 1),
        "max_len": max(lens),
        "min_frag": min(lens),
    }


# ------------------------------------------------------------------------- casos fijos
# Los 4 casos de la tabla de Saruman (2026-08-04), reproducidos con este instrumentador
# de cobertura. Formato frags/perdidos_reales/min_frag:
#   caso                          OLD          FIX_SPEC       FIX_MINIMAL2
#   sep a 600 chars (intermedio)  6/0/1004     5/130/1316     6/0/1004
#   sep a 30 chars (patologico)   33/0/0       2/0/484        2/0/484
#   parrafos sanos                15/0/1282    14/824/434     15/0/1282
#   sin separadores               6/0/1760     6/0/1760       6/0/1760
# OLD figura con perd_real=0: el bug viejo INFLA, no pierde. FIX_SPEC (el guard
# `cut <= i + overlap` con salto `i + step`) PIERDE TEXTO en 2 de los 4 casos, incluido
# el caso SANO: habria mutilado documentos que hoy estan bien.
def caso_sep_intermedio():
    """Separadores cada 600 chars: > overlap(400) pero < step(1448). Es el agujero del
    guard de la spec original: acepta el corte y despues salta el cursor MAS ALLA del
    corte, y el tramo entre cut y el cursor nuevo no lo emite nadie."""
    return ("A" * 600 + "\n\n") * 12


def caso_patologico():
    """Separador a 30 chars del cursor: reproduce el bug (OLD => 33 fragmentos,
    avance de cursor == 1, fragmentos de largo 0)."""
    return "X" * 30 + "\n\n" + "y" * 1900


def caso_parrafos_sanos():
    """Documento sano, parrafos de 640 chars. CONTROL ANTI-REGRESION: es el caso que el
    fix NO debe tocar. FIX_SPEC perdia aca 824 chars de texto real; el fix elegido
    reproduce OLD exactamente."""
    return "\n\n".join("A" * 640 for _ in range(30))


def caso_sin_separadores():
    """Ni '\\n\\n' ni '. ' => cut == -1 en las dos busquedas => cut = end (techo duro)."""
    return "z" * 9000


CASOS_FIJOS = [
    ("sep intermedio (@600)", caso_sep_intermedio()),
    ("patologico (@30)",      caso_patologico()),
    ("parrafos sanos",        caso_parrafos_sanos()),
    ("sin separadores",       caso_sin_separadores()),
]

# Numeros esperados de cada caso fijo BAJO EL FIX: (frags, perdidos_reales, min_frag).
ESPERADO_FIX = {
    "sep intermedio (@600)": (6, 0, 1004),
    "patologico (@30)":      (2, 0, 484),
    "parrafos sanos":        (15, 0, 1282),
    "sin separadores":       (6, 0, 1760),
}


# -------------------------------------------------------------- familias aleatorias
_WORDS = ("cerebro memoria chunk vector indice agente skill tarea vault gateway overlap "
          "corte fragmento separador parrafo oracion cursor progreso guarda invariante").split()


def _sentence(rnd, lo=6, hi=18):
    return " ".join(rnd.choice(_WORDS) for _ in range(rnd.randint(lo, hi))).capitalize() + "."


def fam_parrafos(rnd):
    """F1: markdown normal, parrafos separados por '\\n\\n' de largo variable."""
    n = rnd.randint(4, 40)
    ps = [" ".join(_sentence(rnd) for _ in range(rnd.randint(1, 12))) for _ in range(n)]
    return "\n\n".join(ps)


def fam_solo_oraciones(rnd):
    """F2: un solo bloque, sin '\\n\\n'; solo '. ' como separador."""
    return " ".join(_sentence(rnd) for _ in range(rnd.randint(30, 400)))


def fam_sep_temprano(rnd):
    """F3: separador MUY cerca del inicio + cola larga sin separadores (el patologico)."""
    pos = rnd.randint(1, OVERLAP + MIN_PROGRESS - 1)   # dentro de la zona que el guard rechaza
    cola = rnd.randint(MAX_CHARS, MAX_CHARS * 6)
    sep = rnd.choice(["\n\n", ". "])
    return "q" * pos + sep + "w" * cola


def fam_sin_separadores(rnd):
    """F4: blob puro (base64/hash/tabla sin espacios): cut == -1 siempre."""
    alfa = "abcdef0123456789"
    return "".join(rnd.choice(alfa) for _ in range(rnd.randint(MAX_CHARS + 1, MAX_CHARS * 8)))


def fam_mixto(rnd):
    """F5: parrafos alternados con bloques de codigo largos sin separadores internos."""
    partes = []
    for _ in range(rnd.randint(3, 14)):
        if rnd.random() < 0.5:
            partes.append(" ".join(_sentence(rnd) for _ in range(rnd.randint(1, 8))))
        else:
            partes.append("```\n" + "\n".join(
                "x" * rnd.randint(20, 120) for _ in range(rnd.randint(5, 60))) + "\n```")
    return "\n\n".join(partes)


FAMILIAS = [
    ("F1 parrafos",        fam_parrafos),
    ("F2 solo oraciones",  fam_solo_oraciones),
    ("F3 sep temprano",    fam_sep_temprano),
    ("F4 sin separadores", fam_sin_separadores),
    ("F5 mixto codigo",    fam_mixto),
]
N_POR_FAMILIA = 60
SEED = 20260804          # fijo: los fallos son reproducibles


def _muestras():
    """[(etiqueta, texto)] de los 4 casos fijos + N_POR_FAMILIA por familia aleatoria."""
    out = list(CASOS_FIJOS)
    for nombre, gen in FAMILIAS:
        rnd = random.Random(SEED)
        for k in range(N_POR_FAMILIA):
            t = gen(rnd)
            if len(t) > MAX_CHARS:           # solo interesa el camino multi-fragmento
                out.append((f"{nombre} #{k}", t))
    return out


MUESTRAS = _muestras()


# --------------------------------------------------------------------------- asserts
class TestSplitLargeProgreso(unittest.TestCase):

    def test_a_cero_perdida_de_cobertura(self):
        """(a) Ningun caracter del original queda fuera de todos los fragmentos."""
        malos = []
        for etiqueta, texto in MUESTRAS:
            m = metrics(texto)
            if m["perdidos_reales"]:
                malos.append((etiqueta, m["perdidos_reales"], m["policy"]))
        self.assertEqual(
            malos[:8], [],
            f"PERDIDA DE TEXTO en {len(malos)}/{len(MUESTRAS)} muestras "
            f"(chars reales perdidos, primeras 8): {malos[:8]}. "
            "Sintoma clasico de un salto de cursor que pasa POR ENCIMA del cut "
            "(i = max(cut - overlap, i + N)).")

    def test_b_ningun_fragmento_supera_max_chars(self):
        """(b) MAX_CHARS es el techo del embedder: pasarse = texto invisible al vector."""
        malos = [(e, metrics(t)["max_len"]) for e, t in MUESTRAS
                 if metrics(t)["max_len"] > MAX_CHARS]
        self.assertEqual(malos[:8], [], f"fragmentos por encima de MAX_CHARS={MAX_CHARS}")

    def test_c_cero_pares_con_shift_de_1_char(self):
        """(c) Avance de cursor == 1 es la FIRMA del bug de progreso. Debe fallar con OLD."""
        malos = []
        for etiqueta, texto in MUESTRAS:
            m = metrics(texto)
            if m["shift1"]:
                malos.append((etiqueta, m["shift1"], m["n"]))
        self.assertEqual(
            malos[:8], [],
            f"{len(malos)}/{len(MUESTRAS)} muestras con avance de cursor == 1 "
            f"(etiqueta, n_pares_shift1, n_frags): {malos[:8]}")

    def test_c2_avance_minimo_garantizado(self):
        """(c-bis) Refuerzo: el cursor avanza >= MIN_PROGRESS en TODA iteracion."""
        malos = []
        for etiqueta, texto in MUESTRAS:
            m = metrics(texto)
            if m["n"] > 1 and m["min_avance"] < MIN_PROGRESS:
                malos.append((etiqueta, m["min_avance"]))
        self.assertEqual(malos[:8], [],
                         f"avance del cursor por debajo de MIN_PROGRESS={MIN_PROGRESS}")

    def test_d_caso_patologico_acotado(self):
        """(d) El caso patologico debe dar <= 3 fragmentos (OLD da 33)."""
        m = metrics(caso_patologico())
        self.assertLessEqual(m["n"], 3, f"patologico dio {m['n']} fragmentos: {m}")

    def test_d2_numeros_exactos_de_los_casos_fijos(self):
        """(d-bis) Los 4 casos fijos dan EXACTAMENTE los numeros medidos el 2026-08-04.
        Congela la conducta del fix: cualquier cambio futuro de reparto se ve aca."""
        obtenido = {e: (metrics(t)["n"], metrics(t)["perdidos_reales"], metrics(t)["min_frag"])
                    for e, t in CASOS_FIJOS}
        self.assertEqual(obtenido, ESPERADO_FIX)

    def test_e_caso_sano_no_cambia_vs_old(self):
        """(e) ANTI-REGRESION, el mas importante: en un documento SANO la salida debe ser
        IDENTICA a la del codigo pre-fix. Un fix que reparte distinto los documentos que
        hoy estan bien es un fix que mutila documentos sanos."""
        texto = caso_parrafos_sanos()
        nuevo = split(texto, MAX_CHARS, OVERLAP)
        viejo = split_frozen_old(texto, MAX_CHARS, OVERLAP)
        self.assertEqual(
            [len(x) for x in nuevo], [len(x) for x in viejo],
            f"el caso sano cambio: OLD n={len(viejo)} vs NUEVO n={len(nuevo)}")
        self.assertEqual(nuevo, viejo, "el caso sano cambio de contenido respecto de OLD")

    # ------------------------------------------------------- bordes y contrato interno
    def test_f_borde_cut_menos_uno(self):
        """cut == -1 en ambas busquedas (sin '\\n\\n' ni '. ') => techo duro, sin perdida."""
        t = caso_sin_separadores()
        self.assertNotIn("\n\n", t)
        self.assertNotIn(". ", t)
        m = metrics(t)
        self.assertEqual(m["perdidos"], 0)
        self.assertEqual("".join(split(t, MAX_CHARS, OVERLAP))[:1], t[:1])

    def test_g_bordes_de_longitud(self):
        """<= MAX_CHARS devuelve 1 fragmento intacto; MAX_CHARS+1 se parte sin perdida."""
        for n in (1, 50, MIN_PROGRESS, MAX_CHARS - 1, MAX_CHARS):
            t = "a" * n
            self.assertEqual(split(t, MAX_CHARS, OVERLAP), [t], f"len={n}")
        t = "a" * (MAX_CHARS + 1)
        r = split(t, MAX_CHARS, OVERLAP)
        self.assertGreaterEqual(len(r), 2)
        self.assertEqual(metrics(t)["perdidos"], 0)

    def test_h_min_chars_y_drop_tiny_intactos(self):
        """MIN_CHARS (merge) y _drop_tiny (piso) siguen operando como antes del fix."""
        self.assertEqual(chunker.MIN_CHARS, 200)
        self.assertEqual(chunker.FLOOR_CHARS, 50)
        self.assertEqual(chunker.MAX_CHARS, chunker.EMBED_LIMIT - chunker.BREADCRUMB_BUDGET)
        # merge de seccion chica en la anterior (MIN_CHARS)
        md = "# A\n\n" + ("cuerpo largo de la seccion A. " * 30) + "\n\n## B\n\nchico\n"
        cs = chunker.chunk_markdown(md, "doc")
        self.assertEqual(len(cs), 1, "la seccion chica deberia haberse pegado a la anterior")
        self.assertIn("chico", cs[0].text)
        # _drop_tiny: documento diminuto no se indexa
        self.assertEqual(chunker._drop_tiny([]), [])
        self.assertEqual(chunker.chunk_markdown("# t\n\nnueve car\n", "d"), [])
        # ...pero uno con sustancia sobrevive como unico fragmento
        self.assertEqual(len(chunker.chunk_markdown("# t\n\n" + "s" * 80 + "\n", "d")), 1)
        # ningun chunk emitido supera MAX_CHARS en un doc real multi-seccion
        md2 = "\n\n".join(f"## S{k}\n\n" + caso_parrafos_sanos() for k in range(3))
        for c in chunker.chunk_markdown(md2, "doc"):
            self.assertLessEqual(len(c.text), MAX_CHARS)

    def test_i_instrumentador_reconoce_la_implementacion(self):
        """El instrumentador debe identificar la politica; si no, los numeros no valen.

        Se exige que exista UNA politica que explique TODAS las muestras (interseccion
        no vacia). El empate por muestra es normal: en documentos sanos varias politicas
        producen la misma salida."""
        comun = None
        for etiqueta, texto in MUESTRAS:
            pol = set(metrics(texto)["policy"])
            comun = pol if comun is None else (comun & pol)
            self.assertTrue(comun, f"ninguna politica explica todas las muestras "
                                   f"(se rompio en {etiqueta})")


def _tabla_variantes():
    """Tabla comparativa de las politicas historicas sobre los 4 casos fijos.

    No es un assert: es la EVIDENCIA de por que se eligio esta politica y no la de la
    spec original. Mide con el mismo instrumentador de cobertura, asi que los numeros
    son comparables entre variantes.
    """
    orden = ["OLD", "FIX_SPEC", "FIX_LURTZ", "FIX_MINIMAL", "FIX_MINIMAL2"]
    pol = {n: (g, j) for n, g, j in _POLICIES}
    print("\nCOMPARATIVA DE VARIANTES (frags/perdidos_reales/min_frag)")
    hdr = f"{'CASO FIJO':<24}" + "".join(f"| {k:>16} " for k in orden)
    print(hdr)
    print("-" * len(hdr))
    for etiqueta, texto in CASOS_FIJOS:
        fila = f"{etiqueta:<24}"
        for k in orden:
            g, j = pol[k]
            sp = _spans_with(texto, g, j)
            if sp is None:
                fila += f"| {'NO TERMINA':>16} "
                continue
            cov = bytearray(len(texto))
            for a, b in sp:
                for x in range(a, min(b, len(texto))):
                    cov[x] = 1
            perd = sum(1 for x, v in enumerate(cov) if not v and not texto[x].isspace())
            mn = min(b - a for a, b in sp)
            fila += f"| {f'{len(sp)}/{perd}/{mn}':>16} "
        print(fila)
    print("-" * len(hdr))
    print("perdidos_reales > 0 = PERDIDA DE TEXTO (chars no-whitespace del original que "
          "ningun fragmento emite).")


# ------------------------------------------------------------------------ tabla/reporte
def _tabla():
    print(f"CHUNKER_PATH = {CHUNKER_PATH}")
    print(f"MAX_CHARS={MAX_CHARS} OVERLAP={OVERLAP} MIN_PROGRESS={MIN_PROGRESS} STEP={STEP}")
    comun = None
    for _, texto in MUESTRAS:
        pol = set(metrics(texto)["policy"])
        comun = pol if comun is None else (comun & pol)
    print(f"politica(s) que explican TODAS las muestras = {sorted(comun) or 'NINGUNA'}")
    print(f"politica en el caso patologico = {spans_validated(caso_patologico())[0]}")
    print(f"muestras = {len(MUESTRAS)} (4 fijas + {len(FAMILIAS)}x{N_POR_FAMILIA} "
          f"aleatorias, seed={SEED})\n")
    hdr = f"{'CASO FIJO':<24}| {'frags':>6} | {'perd_real':>9} | {'min_frag':>8} | {'shift1':>6}"
    print(hdr)
    print("-" * len(hdr))
    for etiqueta, texto in CASOS_FIJOS:
        m = metrics(texto)
        print(f"{etiqueta:<24}| {m['n']:>6} | {m['perdidos_reales']:>9} | "
              f"{m['min_frag']:>8} | {m['shift1']:>6}")
    print("-" * len(hdr))
    agg = {}
    for etiqueta, texto in MUESTRAS[len(CASOS_FIJOS):]:
        fam = etiqueta.split(" #")[0]
        m = metrics(texto)
        a = agg.setdefault(fam, {"muestras": 0, "n": 0, "perd": 0, "sh": 0, "over": 0})
        a["muestras"] += 1
        a["n"] += m["n"]
        a["perd"] += m["perdidos_reales"]
        a["sh"] += m["shift1"]
        a["over"] += 1 if m["max_len"] > MAX_CHARS else 0
    print()
    hdr2 = f"{'FAMILIA ALEATORIA':<24}| {'muestras':>8} | {'frags':>6} | {'perd_real':>9} | {'shift1':>6}"
    print(hdr2)
    print("-" * len(hdr2))
    for fam, a in agg.items():
        print(f"{fam:<24}| {a['muestras']:>8} | {a['n']:>6} | {a['perd']:>9} | {a['sh']:>6}")
    print()


if __name__ == "__main__":
    _tabla()
    _tabla_variantes()
    print()
    unittest.main(verbosity=2, exit=True)
