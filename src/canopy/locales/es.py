"""Spanish locale — all 30 keys mirroring en.py."""

STRINGS: dict[str, str] = {
    # UI static labels
    "app_subtitle":   "Consulta los datos de monitoreo de Jocotoco en lenguaje natural.",
    "question_label": "Haga una pregunta",
    "placeholder":    "p. ej. ¿Cuántas especies confirmadas se detectaron en cada reserva en 2023?",
    "run_btn":        "Ejecutar consulta",
    "recent_queries": "### Consultas recientes",
    "clear_btn":      "Borrar historial",
    "tab_answer":     "Respuesta",
    "tab_data":       "Tabla completa",
    "tab_sql":        "Consulta a la base de datos",
    "idle_prompt": (
        "Haga una pregunta sobre los datos de monitoreo de especies.\n\n"
        "→ ¿Cuántas especies confirmadas se detectaron en cada reserva en 2023?  \n"
        "→ ¿Qué sitios tuvieron más actividad el año pasado?  \n"
        "→ Mostrar todas las detecciones de Grallaria ridgelyi desde 2022.  \n"
        "→ ¿Cuántas detecciones están esperando revisión humana en cada sitio?\n\n"
        "---\n\n"
        "**Esta herramienta no puede:**  \n"
        "✗ Evaluar tendencias poblacionales o estado de conservación"
        " — requiere revisión científica formal  \n"
        "✗ Responder preguntas en idiomas distintos al inglés o español  \n"
        "✗ Consultar categorías de la Lista Roja de la UICN"
        " — no están en esta base de datos  \n"
        "✗ Identificar especies por nombre común (p. ej. \"aves\") — use nombres científicos"
    ),
    # Streaming status messages
    "status_reading":       "Leyendo su pregunta…",
    "status_cache_hit":     "Cargando su resultado anterior…",
    "status_understood":    "**Entendí:** {intent}\n\nBuscando en la base de datos…",
    "status_searching_db":  "Buscando en la base de datos de monitoreo…",
    "status_understanding": "Interpretando su pregunta…",
    "status_refining":      "Refinando la búsqueda…",
    # Detection count
    "found_detections_singular": "Se encontró {n} detección — escribiendo su respuesta…",
    "found_detections_plural":   "Se encontraron {n} detecciones — escribiendo su respuesta…",
    # Result row count
    "count_row_singular": "**{n} fila devuelta**",
    "count_row_plural":   "**{n} filas devueltas**",
    # Interpretation block labels (app.py)
    "interpretation_heading":  "Interpretación",
    "interpretation_source":   "Fuente de datos",
    "interpretation_gaps":     "Vacíos",
    "interpretation_gaps_none": "ninguno",
    "interpretation_research": "Preguntas de investigación",
    # Timing footer
    "timing_cached": "⚡ De sus consultas recientes · instantáneo",
    "timing_live":   "Respuesta lista en {total:.0f}s",
    # Error messages
    "error_empty_question": "Por favor, ingrese una pregunta.",
    "error_guard_response": (
        "No pude ejecutar esa consulta de forma segura.\n\n"
        "Esto ocurre a veces con preguntas poco usuales. "
        "Intente preguntar qué contiene la base de datos en lugar de modificarla.\n\n"
        "La consulta generada se muestra en la pestaña "
        "**Consulta a la base de datos** como referencia."
    ),
    "error_guard_status":     "⚠ No se pudo completar la consulta — vea la pestaña Respuesta",
    "error_guard_readonly": (
        "**{operation} no está permitido** — esta herramienta solo puede leer la base de datos, "
        "no modificarla.\n\n"
        "Canopy está diseñado para consultar y analizar datos de monitoreo de especies. "
        "Si desea entender qué contiene la base de datos, intente reformular como una pregunta "
        "(p. ej. *'¿Cuántas detecciones están pendientes?'*"
        " en lugar de pedirle que las modifique).\n\n"
        "La consulta bloqueada se muestra en la pestaña **Consulta a la base de datos**."
    ),
    "error_guard_readonly_status": "⚠ {operation} bloqueado — esta herramienta es de solo lectura",
    "error_timeout": (
        "La consulta a la base de datos tardó demasiado y fue interrumpida.\n\n"
        "Intente preguntar sobre un rango de fechas más pequeño, un sitio específico "
        "o una sola especie en lugar del conjunto de datos completo."
    ),
    "error_iterations": (
        "Esta pregunta requirió demasiados pasos para responderse automáticamente.\n\n"
        "Intente dividirla en preguntas más simples — por ejemplo, pregunte sobre "
        "un sitio o una especie a la vez."
    ),
    "error_db_connection": (
        "No se pudo conectar a la base de datos. Por favor, intente de nuevo en un momento.\n\n"
        "Si el problema persiste, verifique que la conexión a la base de datos esté activa."
    ),
    "error_generic_response": (
        "Ocurrió un error durante la búsqueda. "
        "Por favor, intente de nuevo o reformule su pregunta."
    ),
    "error_generic_status": "⚠ No se pudo completar la consulta",
    "error_unsupported_language": (
        "Canopy actualmente admite preguntas en **inglés o español**.\n\n"
        "Por favor, reformule su pregunta en cualquiera de los dos idiomas — "
        "la herramienta responderá en el que usted use.\n\n"
        "Es posible que se añada compatibilidad con otros idiomas en el futuro."
    ),
    "error_unsupported_language_status": "⚠ Idioma no compatible aún — pruebe en inglés o español",
    # Fuzzy name suggestions (app.py) — shown when a query returns 0 rows and
    # a close match was found for a mistyped species/site name.
    "fuzzy_suggestion_prompt": (
        "No se encontró una coincidencia exacta. ¿Quiso decir alguno de estos?"
    ),
}
