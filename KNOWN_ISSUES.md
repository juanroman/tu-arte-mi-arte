# Known issues — tu-arte-mi-arte

Problemas reales encontrados en pruebas manuales de `root_agent` (vía `adk web`) que no bloquean la v1 pero requieren una decisión de producto más adelante. No es un backlog de features (eso vive en `docs/prd.md` §12) — son defectos concretos, con repro, pendientes de resolver.

---

## 1. La dirección de arte de la casa puede contradecir la escena pedida por el usuario

**Encontrado:** 2026-07-07, conversación "playa en blanco y negro" (modo split, opción A).

**Repro:** pedir un tema que choca con `config/art_direction.toml` (hoy: `palette = "soft pastel, muted saturation"`, `lighting = "soft golden light"`). Ejemplo: "imágenes de la playa en blanco y negro".

**Qué pasa:** `build_prompt()` (`src/engine/art_direction.py`) concatena la cláusula de estilo de la casa a **cada** escena sin condición alguna. El prompt final que llega a Nano Banana 2 contiene instrucciones contradictorias en la misma frase, p. ej.:

> "...Fotografía en blanco y negro... House art direction: fine art photography, with a soft pastel, muted saturation palette, soft golden light..."

**Qué pasó en la prueba:** el modelo resolvió la contradicción a favor del texto explícito de la escena (las tres piezas salieron correctamente en blanco y negro, sin fuga de color). Pero eso es un comportamiento no garantizado del modelo, no del código — nada en `build_prompt` asegura ese resultado, y podría no resolverse igual de limpio con otro tema en conflicto (p. ej. una escena nocturna contra `soft golden light`) o con un cambio de modelo a futuro.

**Más evidencia (mismo día, dos conversaciones adicionales):**
- "animal portraits in the style of Wolf Ademeit" (fondo negro puro, alto contraste dramático) contra `soft pastel, muted saturation, soft golden light` — de nuevo el modelo priorizó el texto explícito de la escena; las tres piezas salieron en b&w de alto contraste sobre negro puro, sin fuga de paleta.
- "escenas de navidad con lego" (rojos y verdes muy saturados, luces cálidas vibrantes) contra `muted saturation` — conflicto en la dirección opuesta (saturado vs. apagado), incluido explícitamente en la escena ("gemas de plástico translúcido que brillan", "luz LED cálida"); de nuevo el modelo favoreció el texto de la escena sobre la paleta apagada de la casa.

Con estos tres casos (monocromo vs. pastel, alto contraste vs. pastel, saturado vs. apagado) el patrón empírico de "el texto explícito de la escena gana" es más amplio de lo que parecía con un solo caso — pero sigue siendo un comportamiento observado del modelo, no una garantía del código.

**Por qué importa:** hoy la dirección de arte es todo-o-nada por request; no hay manera de que la escena override o suprima selectivamente un campo del estilo de casa (paleta sí, tono no, etc.), y el agente no tiene forma de saber que está mandando una instrucción contradictoria.

**Posibles arreglos (sin decidir):**
- Barato: enseñarle a `root_agent` a detectar el choque y decirlo explícitamente en la escena ("ignora la paleta de la casa, esta pieza es en blanco y negro"), haciendo el override intencional en vez de accidental.
- Más correcto: mecanismo en `build_prompt`/agente para override selectivo de campos del estilo por request, y/o exponerle al usuario qué estilo de casa está activo y cuándo su pedido choca con él, antes de generar.

**Estado:** sin resolver, v1 no bloqueada por esto — el caso de prueba real no mostró fuga de color, pero el mecanismo no lo garantiza.

---

## 2. El modelo no siempre respeta instrucciones de encuadre/framing específicas

**Encontrado:** 2026-07-07, conversación "escenas de navidad con lego".

**Repro:** el agente pidió explícitamente en `scene_43l` un "primer plano macro" de un calcetín navideño colgado de una chimenea.

**Qué pasó:** la imagen generada para 43L salió como una toma ambiental más abierta (se ve ventana, piso y contexto de la habitación completa), no el close-up macro pedido. Es una toma válida y distinta de las otras dos del conjunto (el objetivo de diversidad de archetypes sí se cumplió), pero no es la toma específica que el prompt pidió.

**Por qué importa:** distinto de los problemas de contenido/estilo ya documentados — aquí el modelo simplemente no siguió una instrucción de encuadre concreta ("macro", "primer plano"), aunque sí respetó el sujeto y la escena. Sugiere que palabras clave de framing no son tan confiables como el contenido/estilo explícito para controlar la composición final.

**Posibles arreglos evaluados:** comparamos, con `scripts/eval_framing.py`
(3 sujetos, keyword vs. frase verbosa estilo guía de prompting de Google —
"tomada desde una perspectiva macro extrema con un lente macro de enfoque
cercano..."), llamando a `generate_image` directamente para aislar el
fraseo de la variabilidad propia de cómo el agente redacta la escena. La
frase verbosa promedió framing_match=3.00/5 ([3,2,4]) contra 2.33/5
([2,2,3]) de la frase corta "primer plano macro", y fue la única variante
que produjo un `is_tight_macro=True`. Es una ventaja modesta y consistente
en dirección, no una corrección: con n=3 por variante y 5 de 6
generaciones totales sin llegar a macro genuino, el problema de fondo
(Nano Banana 2 no sigue el encuadre de forma confiable) sigue sin
resolverse.

**Mitigación parcial aplicada (2026-07-14):** se reforzó únicamente la
entrada `macro/detalle` del menú de archetypes en `agent.py` (ETAPA 2) con
la frase verbosa validada arriba (perspectiva + tipo de lente + qué llena
el cuadro), dejando sin cambio las otras 8 entradas del menú y el
principio de "guía, no lista cerrada". Se corrió `scripts/eval_framing.py
--agent` antes y después del cambio (`data/evals/framing_eval_agent_before.json`
/ `_after.json`, no versionados) con el tema "adornos navideños de fieltro
colgados de una chimenea de piedra" para verificar la escena real que
autora `root_agent`: el panel 43L (el que el agente eligió como
macro/detalle en ambas corridas) subió de framing_match=3/5 a 4/5, pero
`is_tight_macro` siguió en `False` en ambas — mejora perceptible en qué
tan cerrada es la toma, sin llegar a macro genuino. Los otros dos paneles
(43R, 50) no cambiaron (2/5 y 1/5), como se esperaba, ya que no usan ese
archetype. `scripts/eval_coherence.py` (5 temas) confirmó que
`archetype_diversity` se mantuvo en 5.00/5 — no hay señal de que el
agente se incline de más hacia macro por tener ahora una descripción más
rica.

**Estado:** mitigación parcial aplicada, sin confirmar como solución —
evidencia de n pequeño sugiere una mejora modesta con fraseo verboso, pero
no garantiza encuadre macro consistente. V1 sigue sin bloquearse por esto;
requeriría más repros con temas variados para confirmar si el problema de
fondo persiste con la nueva redacción.

---

## 3. El modelo a veces termina un turno con texto vacío justo después de llamar una tool

**Encontrado:** 2026-07-15, conversación real exportada de `adk web` (`weekend2.json`), mensaje "quiero diseñar un lote nuevo para el fin de semana" (ya sin ambigüedad de intención, un solo mensaje claro).

**Repro:** `root_agent` llamó correctamente `list_skills` → `load_skill("galeria-por-lotes")` en el mismo turno, pero la respuesta final del modelo trajo `finishReason: "STOP"` con un texto vacío (`""`) y **sin `candidatesTokenCount` en `usageMetadata`** — el modelo devolvió cero tokens de salida, no un error retornado por la SDK ni una excepción capturable. El usuario se quedó sin ninguna respuesta visible hasta que mandó un mensaje de seguimiento ("estas ahí?") en un turno posterior, momento en el que el modelo sí produjo la pregunta de disambiguación esperada (sábado 18/domingo 19 vs. miércoles 15-domingo 19).

**Por qué importa:** es indistinguible, desde la UI, de que el bot se haya colgado — nada en el chat indica que el turno "terminó bien" sin decir nada.

**Intentos de reproducir a voluntad (TDD, 2026-07-15):** se escribió `scripts/eval_batch_load_skill_followup.py`, que repite el mensaje exacto del repro contra el agente real y verifica que el texto final del turno no quede vacío. Corrido 50 veces contra la API real (fuera de esta conversación), **nunca reprodujo el texto vacío** — confirma que es un evento estocástico de baja probabilidad a nivel de API/modelo, no algo que un cambio de prompt o de frecuencia de repetición pueda forzar de forma determinística.

**Por qué no se resolvió con reintento de código:** la opción más robusta (detectar la respuesta vacía vía `after_model_callback` y reintentar automáticamente la llamada al modelo) se descartó explícitamente — decisión del usuario de no interceptar/reescribir las respuestas del modelo ("no quiero jugar a ser dios con los mensajes"). Queda fuera de alcance mientras esa postura no cambie.

**Mitigación aplicada:** párrafo nuevo al inicio de `_BASE_INSTRUCTION` en `agent.py` — "Nunca termines un turno sin texto visible para el usuario, especialmente justo después de llamar una tool... una llamada de tool nunca es, por sí sola, una respuesta completa." Es una instrucción de mejor esfuerzo: puede reducir los casos en que el modelo *elige* terminar el turno tras solo la llamada a la tool, pero no puede prevenir una respuesta de la API con cero tokens de salida — esa causa está fuera del control de cualquier prompt. Cubierto solo por un test estructural (`test_root_agent_instruction_forbids_empty_text_after_a_tool_call` en `tests/test_agent_smoke.py`: el párrafo existe en la instrucción), no por un test de comportamiento real — no hay forma barata y determinística de forzar una respuesta vacía real del modelo bajo pytest.

**Estado:** sin resolver de raíz — riesgo aceptado y documentado, mismo tratamiento que los issues #1 y #2 de este archivo. Si se observa que ocurre con más frecuencia de la esperada, reconsiderar la opción de reintento por código (`after_model_callback`) descartada arriba.

---

## 4. El reporte proactivo de un lote puede crashear si una imagen 4K final supera el límite de tamaño de foto de Telegram

**Encontrado:** 2026-07-18, demo de cierre de la Etapa 4 (dev_plan_phase_2.md §4.3) — lote real de 2 días, tema "Invernaderos botánicos" (`batch_id=batch_2a185ece`), corrido de punta a punta contra el bot local de Telegram vía `web.telegram.org`.

**Repro:** el motor de lote terminó con éxito total (draft 5/5, finalización 4K 5/5, subida a TV 6/6, rotación configurada en las 3 TVs) — ninguna falla real en ninguna etapa del corredor. Al mandar el reporte proactivo final (`_send_batch_report`, dev_plan §3.2), 2 de las 6 imágenes finalizadas en 4K pesaban 11.1MB y 11.4MB (el resto entre 0.8MB y 9.7MB). `bot.send_media_group` lanzó `telegram.error.BadRequest: File of size 11412284 bytes is too big for a photo; the maximum size is 10485760 bytes` (límite duro de la API de Telegram, no configurable).

**Qué pasó:** la excepción escapó de `_send_batch_report` sin capturarse, se propagó a través de `Application.create_task` hasta `global_error_handler` (mismo mecanismo ya validado en dev_plan §3.1 para excepciones reales del corredor de fondo) — el proceso no se cayó, quedó registrada en el log, pero el usuario nunca recibió ni el texto ni el álbum de fotos de ese lote. `batch.status` se quedó en `'running'` (nunca llegó a `'reported'`), así que `reconcile_batches_on_startup` (dev_plan §3.3) sí lo recogería como no-terminal en el próximo arranque del bot y reintentaría mandar el reporte — pero como el archivo de imagen sigue pesando lo mismo, el reintento fallaría exactamente igual, en un bucle silencioso hasta que alguien reinicie el bot lo suficientes veces como para notarlo en el log.

**Por qué importa:** `_batch_report_albums`/`_send_batch_report` (Etapa 3.2) nunca contemplaron el tamaño de archivo — solo paginan por *cantidad* de fotos (máximo 10 por álbum, requisito duro #8), no por *peso*. Una imagen 4K finalizada (`generate_final_high_res`, PRD §7.7) no tiene un techo de tamaño garantizado; picos de complejidad visual (ambos casos de este repro eran paneles con mucho detalle: follaje/hojas) pueden superar los 10MB de Telegram sin que nada en el corredor lo detecte antes de intentar mandarlo.

**Por qué no se arregló en el momento:** encontrado durante la demo de cierre de la Etapa 4/extensión completa (dev_plan_phase_2.md §4.3) — decisión explícita de la sesión de documentarlo aquí en vez de bloquear el cierre de la extensión con un fix no planeado; el motor de lote en sí (el riesgo real de esta extensión) funcionó de punta a punta sin fallas.

**Posibles arreglos (sin decidir):** (a) detectar en `_batch_report_albums` cualquier imagen que exceda el límite de Telegram y mandarla como documento (`send_document`, sin el límite de 10MB) en vez de como foto dentro del álbum; (b) comprimir/re-encodear a menor calidad JPEG solo para el envío del reporte, preservando el archivo original en disco; (c) detectar el caso en `_send_batch_report` y, si falla por tamaño, reintentar ese álbum específico con las imágenes problemáticas degradadas, en vez de dejar todo el envío en un bucle de reintento idéntico.

**Estado:** sin resolver — riesgo aceptado y documentado, mismo tratamiento que los issues #1-#3 de este archivo. El `batch_id=batch_2a185ece` de este repro quedó con `status='running'` en `data/batch.sqlite3`; su reporte se reintentará (y volverá a fallar) en cada arranque del bot hasta que este issue se resuelva o el lote se marque manualmente como `'reported'`.
