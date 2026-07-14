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
