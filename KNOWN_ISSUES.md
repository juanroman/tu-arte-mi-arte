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

**Posibles arreglos (sin decidir):** ninguno evaluado todavía; requiere más repros para saber si es consistente con ciertos sujetos/escenas o fue un caso aislado.

**Estado:** sin resolver, v1 no bloqueada por esto — un solo caso observado, no confirmado como patrón.

---

## 3. Elección de matte (marco de color) sin decidir

**Encontrado:** 2026-07-08, spike de write path (`docs/dev_plan.md` §3.1, `scripts/spike_tv_write_path.py`).

**Qué pasa:** `samsungtvws` (`SamsungTVArt.upload()`) acepta `matte`/`portrait_matte` para elegir el marco de color que Art Mode dibuja alrededor de la pieza (también expone `get_matte_list()`/`change_matte()` para listar opciones y cambiarlo después de subir). El spike de 3.1 hardcodeó `matte="none", portrait_matte="none"` únicamente para tener una subida de prueba limpia — no es una decisión de producto, fue la opción más simple para validar que `upload()`/`select_image()` funcionan.

**Por qué importa:** ni el PRD (§7.6 Despliegue a las TVs) ni `docs/dev_plan.md` tienen una iteración que decida qué matte usar por TV (o si debe ser configurable). Sin decisión, 3.3 (despliegue completo a las dos 43") heredaría el `"none"` del spike por inercia, no por elección.

**Posibles arreglos (sin decidir):**
- Definir un matte fijo por instalación (o "none") como parte de `config/room.toml` o un nuevo `config/tv.toml`, mismo patrón editable que el resto de la config de casa.
- Exponerlo como parámetro configurable por TV si el usuario quiere distinto marco en 43L/43R vs. 50.

**Estado:** sin resolver, v1 no bloqueada por esto — el default `"none"` del spike es un placeholder de prueba, no una decisión tomada. Retomar en 3.3.
