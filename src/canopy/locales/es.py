"""Spanish locale — all 23 keys mirroring en.py."""

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
        "→ ¿Cuántas detecciones están esperando revisión humana en cada sitio?"
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
    "error_generic_response": (
        "Ocurrió un error durante la búsqueda. "
        "Por favor, intente de nuevo o reformule su pregunta."
    ),
    "error_generic_status": "⚠ No se pudo completar la consulta",
}
