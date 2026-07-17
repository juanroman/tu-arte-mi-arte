---
name: galeria-por-lotes
description: >
  ÚSALA cuando el usuario mencione explícitamente MÁS DE UN DÍA u ocasión
  para el arte que va a pedir — cualquier frase que indique que el
  contenido va a cambiar con el tiempo (por día, por semana, para varios
  días, para toda la semana, uno distinto cada día, N imágenes/piezas para
  N días), sin importar qué otra palabra use junto a eso (galería, lote,
  colección, conjunto, trabajo). Ejemplos que SÍ deben activarla: "algo
  diferente cada día toda la semana", "quiero 10 imágenes de este tema, una
  por día", "hazme un conjunto para varios días con bicicletas vintage",
  "una colección de otoño para toda la semana". Si detectas la frase
  "varios días", "cada día" o "toda la semana" en el pedido, actívala.
  NO la actives si el pedido es para HOY/AHORA sin mención de más de un
  día (una sola pieza o el conjunto normal de las tres pantallas), aunque
  el usuario lo llame "colección" o "conjunto" — ese sigue siendo el flujo
  por defecto sin esta skill.
metadata:
  adk_additional_tools:
    ["preview_batch_day", "materialize_batch_gallery", "estimate_batch_duration"]
---

Estás en modo de arte para varios días.

## Paso 2 — Propuesta de agrupación en sub-temas (PRD §15.3 paso 2)

Determina primero, a partir de lo que el usuario ya dijo en la conversación:

- **El tema general** de la galería.
- **El número de días (N):** si el usuario no lo especificó con una
  referencia de tiempo (ni un conteo explícito ni una expresión relativa
  como "fin de semana"), asume 7.

### Resolución de referencias relativas de tiempo

Si el usuario expresa el rango en días con una frase relativa en vez de
un conteo directo, resuélvela contra la fecha actual (bloque "FECHA Y
HORA ACTUAL" de la instrucción de root_agent) — nunca la adivines ni la
calcules sin esa fecha real de por medio. Reglas exactas:

- **"este fin de semana" / "el fin de semana":**
  - Si hoy es **viernes, sábado o domingo**, no hay ambigüedad — resuelve
    directo al sábado y domingo de esta semana (o solo los días que
    falten, si hoy ya es sábado o domingo), sin preguntar nada.
  - Si hoy es **lunes a jueves**, SÍ hay ambigüedad real entre "solo
    sábado y domingo" (2 días) y "desde hoy hasta el domingo" (más días)
    — **pregunta explícitamente, citando las fechas concretas de ambas
    opciones** (p. ej., si hoy es jueves 16 de julio: "¿te refieres solo
    a sábado 18 y domingo 19 (2 días), o prefieres que el lote arranque
    desde hoy jueves y cubra hasta el domingo (4 días)?"). Nunca elijas
    una de las dos en silencio — es la única situación de este paso 2
    donde la agrupación espera una respuesta antes de proponerse.
- **"la próxima semana":** los 7 días empezando el próximo lunes (si hoy
  ya es lunes, sigue siendo la semana que INICIA el próximo lunes, no la
  actual). Sin ambigüedad, no hace falta preguntar.
- **"de aquí al viernes" / "los próximos N días":** cuenta literal desde
  hoy (inclusive) hasta la fecha mencionada, o exactamente N días
  consecutivos empezando hoy. Sin ambigüedad, no hace falta preguntar.

Después (ya con N y el rango de fechas resueltos), **sin llamar ninguna
tool todavía**, responde en el chat con una
propuesta de estructura de sub-grupos dentro de ese tema. Reglas de la
propuesta:

- Entre **2 y 4 sub-grupos** que en conjunto cubran exactamente los N días
  pedidos, sin traslapes ni huecos — cada día pertenece a un solo
  sub-grupo, y la suma de días de todos los sub-grupos debe ser igual a N.
- Cada sub-grupo agrupa entre **2 y 4 días consecutivos**.
- Cada sub-grupo debe tener un **ángulo/enfoque visualmente distinto**
  dentro del mismo tema general, sin traslape conceptual con los demás
  sub-grupos — inspirado en el patrón histórico documentado en el PRD
  (Apéndice C: sub-temas + contraste de composición dentro de un tema
  mayor), pero un solo tema para todo el lote, nunca uno distinto por
  sub-grupo, y sin reproducir el enfoque de galería del Art Store que ese
  flujo manual usaba para la pantalla de 50" (aquí las tres pantallas se
  generan siempre por IA, nunca con una galería de fábrica).
- Nombra cada sub-grupo con una etiqueta corta y descriptiva (2-5
  palabras) que capture su ángulo distintivo, e indica explícitamente qué
  días cubre.
- Presenta la propuesta completa en un solo mensaje (los N días de una
  sola vez), no sub-grupo por sub-grupo.

Formato de ejemplo (tema "Primavera", 7 días):

> Para 7 días de Primavera, propongo 3 sub-grupos:
> 1. Pétalos y túneles de flores (días 1-2)
> 2. Escenas urbanas de primavera (días 3-4)
> 3. Hora dorada (días 5-7)
>
> ¿Te parece bien esta estructura, o prefieres ajustar algo?

## Aprobación de la agrupación (PRD §15.3 paso 3)

Espera la aprobación explícita del usuario (p. ej. "sí", "así está bien",
"perfecto") antes de avanzar al siguiente paso (redacción de prompts por
sub-grupo, iteración futura de este plan). Si el usuario pide un ajuste
(mover un sub-grupo, cambiar su rango de días, renombrarlo, fusionar o
dividir sub-grupos), aplica el cambio directamente sobre la propuesta ya
hecha y vuelve a presentar la estructura completa actualizada — nunca
reinicies la conversación desde cero ni descartes lo ya acordado, salvo
que el usuario lo pida explícitamente.

## Paso 4 — Prompts por sub-grupo (PRD §15.3 paso 4)

En cuanto el usuario apruebe un sub-grupo (paso 3), continúa de inmediato
con ese sub-grupo — no esperes un disparador nuevo del usuario. Redacta,
día por día, las escenas de **ese sub-grupo únicamente**; nunca adelantes
sub-grupos todavía no aprobados.

Por cada día del sub-grupo, decide primero el modo:

- **Modo independiente/díptico (3 escenas: 43L, 43R, 50):** el default
  cuando no hay un motivo claro de continuidad — p. ej. un retrato, un
  detalle/macro, o una escena donde cada panel gana más siendo una
  composición autónoma.
- **Modo split (2 escenas: wide, 50):** solo cuando la escena se
  beneficia de una sola composición continua entre las dos verticales —
  p. ej. un horizonte, un paisaje panorámico, una escena arquitectónica
  que pierde sentido partida en dos encuadres independientes.
- La decisión es exclusivamente por el contenido de esa escena
  específica, nunca una cuota — orientación agregada ~70/30
  independiente/split a través de todo el lote, pero no forzada por
  sub-grupo ni por día.
- Cuando un día use modo split, indica en una sola frase por qué esa
  escena se beneficia de continuidad — para que la elección sea
  auditable por el usuario, no una caja negra.

Redacta cada escena con la misma fórmula y las mismas reglas que ya usa
`root_agent` para un conjunto suelto (PRD §7.7): sujeto + acción +
lugar/escena + composición/encuadre + luz, en prosa. No uses lenguaje de
adyacencia o layout ("a la derecha de", "continúa la escena") ni
menciones aspect ratio o flags de cámara. Elige un tipo de plano
(archetype) distinto para cada panel dentro del mismo día — usa como
guía el mismo menú: macro/detalle, plano general abierto/paisaje, figura
humana en la escena, silueta, textura/abstracto en close-up,
aéreo/elevado, reflejo/agua, líneas que guían la mirada, luz
dorada/contraluz. Para el panel 50, inclínate por un plano general
abierto.

Presenta las escenas de **un solo sub-grupo por mensaje** (todos sus
días juntos), nunca todos los sub-grupos ni todos los N días del lote de
una sola vez.

## Aprobación de prompts (PRD §15.3 paso 5)

Espera la aprobación explícita del sub-grupo actual antes de continuar
con el siguiente sub-grupo — nunca redactes el siguiente sub-grupo sin
que el actual esté aprobado.

Si el usuario pide un cambio a un solo día del sub-grupo (p. ej. "cambia
el día 2 a algo más cerrado"), reescribe solo ese día y conserva los
demás tal como estaban — mismo principio que la aprobación de
agrupación (paso 3): nunca reinicies ni redactes de cero el sub-grupo
completo por un ajuste puntual.

## Paso 6 — Preview (PRD §15.3 paso 6)

En cuanto se apruebe un sub-grupo (paso 5), sin esperar un disparador
nuevo del usuario, genera el preview del **primer día de ese sub-grupo**
llamando `preview_batch_day` una sola vez, con el `mode` y las escenas
exactas ya redactadas y aprobadas en el paso 4 para ese día — nunca
inventes ni reescribas una escena nueva aquí, ni adelantes días
posteriores del mismo sub-grupo.

Si el usuario pide explícitamente el preview del sub-grupo completo
(p. ej. "preview del sub-grupo completo", "muéstrame todos los días"),
llama `preview_batch_day` una vez por cada día de ese sub-grupo, en el
mismo turno, cada llamada con el `mode` y las escenas de su día
correspondiente.

`preview_batch_day` devuelve el mismo tipo de resultado que
`generate_set_diptico`/`generate_set_split`: puede traer algunos
paneles ya completados junto con, como máximo, un panel con error.
Aplica el mismo criterio ya usado para conjuntos sueltos (RESULTADOS
MIXTOS, instrucción de `root_agent`) — nunca descartes ni dejes de
confirmar un `image_id` generado, nunca generalices el fallo de un
panel/día a todo el sub-grupo, y sé preciso sobre cuál día/panel
específico falló.

Esta iteración solo produce drafts en baja resolución (1K), en disco —
no incluye finalización 4K, subida a TV, ni el estimado de tiempo
(llegan con el motor de lote, iteraciones posteriores de este plan).

Después del preview: si quedan sub-grupos sin redactar/aprobar, continúa
con el paso 4 del siguiente sub-grupo. Si el sub-grupo recién
previsualizado era el último, confirma que todos los sub-grupos del
lote quedaron con prompts aprobados y sus previews disponibles, y
continúa con el paso 7 (estimado de tiempo).

## Paso 7 — Estimado de tiempo (PRD §15.3 paso 7)

Una vez que TODOS los sub-grupos del lote tienen sus prompts aprobados
(paso 5) — sin importar si se pidió o no el preview de cada uno —,
llama `estimate_batch_duration` **una sola vez**, pasando `day_modes`
como una lista con el `mode` ('independiente' o 'split') de cada uno de
los N días del lote, en el orden del lote (día 1 primero) — el mismo
valor ya decidido para cada día en el paso 4, nunca inventado aquí.

Comunica el resultado (`estimated_minutes`) al usuario en una frase
breve, en lenguaje natural (p. ej. "esto tomará aproximadamente 25
minutos"), antes de pedir la confirmación del paso 8 — nunca como
promesa exacta, es un estimado. Si `estimate_batch_duration` devuelve
`'error'`, no bloquees el flujo por eso: informa al usuario que no se
pudo calcular el estimado y continúa igual con el paso 8.

## Paso 8 — Confirmación y materialización del lote (PRD §15.3 paso 8, parcial)

Una vez comunicado el estimado de tiempo (paso 7), pregunta al usuario
si confirma el lote completo para que el motor de lote lo procese
(p. ej. "¿confirmas este lote de N días para generarlo?"). Espera su
aprobación explícita antes de continuar.

Al confirmar, llama `materialize_batch_gallery` **una sola vez** con
`theme` (el tema general del lote) y `days`: una lista con un dict por
cada uno de los N días del lote, en orden, usando exactamente las
escenas ya redactadas y aprobadas en el paso 4 para cada día — nunca
inventes ni reescribas una escena aquí. Cada dict de `days` lleva
`day_index` (1-based), `mode` ('independiente' o 'split'), `sub_group`
(el nombre de su sub-grupo), y `prompts` ({'43L','43R','50'} para
independiente, {'wide','50'} para split).

Si `materialize_batch_gallery` devuelve `'error'`, informa el problema
al usuario en una frase clara (p. ej. si falta un día o un prompt) y no
insistas por tu cuenta — corrige lo que falte con el usuario y vuelve a
llamarla. Si devuelve `batch_id`, confirma al usuario que el lote quedó
guardado con ese identificador, y aclara que la generación real (drafts
en baja resolución, finalización en 4K, subida a las TVs) llega con una
iteración posterior de este proyecto — esta iteración solo persiste el
lote, no lo procesa todavía.
