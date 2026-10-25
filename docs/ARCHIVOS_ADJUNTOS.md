# Archivos del chat privado

Recarga con Ctrl+F5, abre **Mi agente** y usa **Subir archivos** en el costado o **＋** junto al mensaje. Elegir un archivo sin conversación crea una con el perfil/proveedor seleccionados. También puedes arrastrar archivos al panel y pegar imágenes en el cuadro del mensaje. Una vez cargados, escribe tu pregunta. El panel conserva nombres, tamaño, vistas previas y descarga del original.

Los adjuntos se guardan en SQLite (`agent_attachments`) dentro de su conversación. Su contenido no aparece en el canal principal ni en otros chats o cuentas. La subida, eliminación y lectura comprueban sesión y propietario; las modificaciones exigen CSRF. Mientras hay un trabajo en cola o ejecutándose, los archivos de entrada permanecen estables: termina o cancela ese trabajo antes de cambiarlos.

## Lectores y alcance

| Formato | Lectura |
|---|---|
| TXT, Markdown, código, CSV/TSV, JSON/YAML, XML y otros textos | UTF-8, UTF-16 con BOM y alternativa CP1252; selección de fragmentos pertinentes |
| HTML | Texto extraído sin ejecutar scripts ni estilos |
| PDF | Texto de hasta 50 páginas, con etiquetas de página; vista previa y análisis visual de páginas escaneadas o solicitadas |
| DOCX | Texto de párrafos y tablas; imágenes incrustadas sin análisis visual automático |
| XLSX | Celdas y valores guardados; fórmulas mostradas como texto, sin ejecutar ni recalcular |
| PPTX | Texto de hasta 50 diapositivas; imágenes incrustadas sin análisis visual automático |
| PNG, JPEG, WebP, GIF, BMP, TIFF, ICO y AVIF | Imagen normalizada para el modelo visual; primer fotograma en formatos de varias imágenes |

Los archivos binarios antiguos DOC/XLS/PPT, HEIC, archivos comprimidos, ejecutables, audio y vídeo no tienen lector de análisis en esta implementación. El mensaje de error indica convertirlos o usar un formato admitido. Las descargas multimedia conservan su panel y funcionamiento independientes.

Cada archivo permite hasta 20 MB; una conversación admite 20 archivos y 100 MB, con 250 MB por cuenta. Se conservan hasta 200000 caracteres extraídos por archivo. Los lectores corren en procesos separados con 25 segundos de plazo y hasta dos lectores simultáneos. Los documentos Office tienen límites sobre su contenido descomprimido. Un formato/extensión incorrectos o un PDF con contraseña se rechazan con un mensaje concreto.

El contexto del modelo contiene fragmentos acotados y referencias a sus archivos. `attachment_read` permite listar adjuntos y leer más texto mediante `attachment_id`, `offset` y `next_offset`. Subir un documento largo no significa que cada respuesta haya utilizado todo su contenido. El panel muestra límites de extracción; las respuestas informan cuando se agota el alcance visual.

## Visión local

`config/agent.json` contiene el proveedor especialista `vision`, con `purpose: vision` y `qwen3-vl:2b-instruct`. No aparece en el selector de proveedores de chat: la aplicación lo consulta para imágenes y páginas de PDF. `attachments.vision_backend` permite seleccionar otro proveedor preparado para visión. El chat sigue usando su proveedor habitual y recibe las observaciones visuales con sus referencias.

El modelo visual ya se descargó en este equipo. Para preparar otra instalación:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
ollama pull qwen3-vl:2b-instruct
```

Las imágenes se normalizan como JPEG y los datos EXIF no se envían al modelo visual. Se rechazan imágenes de más de 20 millones de píxeles. Se analizan hasta tres vistas por mensaje; para un PDF puedes indicar «página 4» dentro de las primeras 50 páginas. Los escaneos se interpretan con visión, no se presentan como una transcripción garantizada. Texto pequeño, borroso o recortado puede ser ilegible.

Con la configuración actual ambos modelos son locales. Si eliges una API externa para el chat, recibirá los fragmentos y observaciones pertinentes del archivo. Configurar un proveedor visual externo le enviaría las imágenes seleccionadas. El contenido se trata como datos no confiables y no puede ampliar las herramientas ni permisos del agente.

Referencias primarias: [API de visión de Ollama](https://docs.ollama.com/capabilities/vision), [modelo Qwen3-VL 2B Instruct](https://ollama.com/library/qwen3-vl:2b-instruct) y [extracción de texto con PyMuPDF](https://pymupdf.readthedocs.io/en/latest/recipes-text.html).

## Verificación

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p test_attachments.py -v
node tests/test_attachments_frontend.cjs
.\.venv\Scripts\python.exe -u -X utf8 scripts/check_attachments.py
```

La prueba real utiliza una cuenta y SQLite temporales; no crea mensajes en la cuenta de uso diario. Comprueba una clave al final de un Markdown largo, el total de un PDF, número/color en una imagen y texto visible en un PDF escaneado. Conserva resultados en `.local/attachment-checks/live.json`. Las pruebas simuladas adicionales cubren privacidad, CSRF, archivos dañados, colas, límites, texto fuera del primer fragmento, selección entre 20 archivos y errores de visión.
