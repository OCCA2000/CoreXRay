import azure.functions as func
import json
import io
import re
from fpdf import FPDF
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError
import os

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

# --- FUNCTION 1: The COBOL Cleaner ---
@app.function_name(name="CleanData")
@app.route("", methods=["POST"])
def CleanData(req: func.HttpRequest) -> func.HttpResponse:
    try:
        req_body = req.get_json()
        raw_text = req_body.get('text', '')
        lines = raw_text.split('\n')

        # Contadores para cada campo
        call_counts = {}
        copybook_counts = {}
        file_counts = {}
        table_counts = {}
        cics_counts = {}

        file_suffixes = [
            '-FILE-IN', '-FILE-OUT',
            '-ARCHIVO-IN', '-ARCHIVO-OUT',
            '-FILE', '-ARCHIVO',
            '-INPUT', '-OUTPUT',
            '-IN', '-OUT'
        ]

        def remove_inline_comment(line):
            in_string = False
            quote_char = None
            i = 0
            while i < len(line):
                char = line[i]
                if char in ('"', "'") and not in_string:
                    in_string = True
                    quote_char = char
                elif char == quote_char and in_string:
                    if i + 1 < len(line) and line[i + 1] == quote_char:
                        i += 2
                        continue
                    else:
                        in_string = False
                        quote_char = None
                elif not in_string and char == '*' \
                        and i + 1 < len(line) and line[i + 1] == '>':
                    return line[:i].strip()
                i += 1
            return line.strip()

        def extract_tables_from_sql(sql):
            tables = set()
            for m in re.finditer(r'\bFROM\s+([\w-]+)', sql, re.IGNORECASE):
                name = m.group(1)
                if name.upper() not in ('SELECT', 'WHERE', 'SET', '('):
                    tables.add(name)
            for m in re.finditer(
                r'\b(?:INNER|LEFT|RIGHT|FULL|CROSS)?\s*'
                r'(?:OUTER\s+)?JOIN\s+([\w-]+)',
                sql, re.IGNORECASE
            ):
                tables.add(m.group(1))
            for m in re.finditer(
                r'\bINSERT\s+INTO\s+([\w-]+)', sql, re.IGNORECASE
            ):
                tables.add(m.group(1))
            for m in re.finditer(
                r'\bUPDATE\s+([\w-]+)', sql, re.IGNORECASE
            ):
                tables.add(m.group(1))
            for m in re.finditer(
                r'\bDELETE\s+FROM\s+([\w-]+)', sql, re.IGNORECASE
            ):
                tables.add(m.group(1))
            return tables

        in_sql_block = False
        sql_block = []

        for line in lines:
            stripped = line.strip()

            if not stripped:
                continue
            if stripped.startswith('*'):
                continue
            if stripped.startswith('/'):
                continue
            if len(line) > 6 and line[6] == '-':
                continue

            stripped = remove_inline_comment(stripped)
            if not stripped:
                continue

            if re.search(r'\bEXEC\s+SQL\b', stripped, re.IGNORECASE):
                in_sql_block = True
                sql_block = []
                continue

            if re.search(r'\bEND-EXEC\b', stripped, re.IGNORECASE):
                if in_sql_block and sql_block:
                    full_sql = ' '.join(sql_block)
                    found_tables = extract_tables_from_sql(full_sql)
                    for table in found_tables:
                        table_counts[table] = table_counts.get(table, 0) + 1
                in_sql_block = False
                sql_block = []
                continue

            if in_sql_block:
                sql_block.append(stripped)
                continue

            # CALL
            m = re.search(r"CALL\s+['\"]?([\w-]+)['\"]?",
                         stripped, re.IGNORECASE)
            if m:
                name = m.group(1)
                call_counts[name] = call_counts.get(name, 0) + 1

            # COPY
            m = re.search(r"COPY\s+([\w-]+)",
                         stripped, re.IGNORECASE)
            if m:
                name = m.group(1)
                copybook_counts[name] = copybook_counts.get(name, 0) + 1

            # FD
            m = re.search(r"\bFD\s+([\w-]+)",
                         stripped, re.IGNORECASE)
            if m:
                file_name = m.group(1)
                for suffix in file_suffixes:
                    if file_name.upper().endswith(suffix.upper()):
                        file_name = file_name[:len(file_name)-len(suffix)]
                        break
                file_counts[file_name] = file_counts.get(file_name, 0) + 1

            # TRANSID
            m = re.search(r"TRANSID\s*\(?['\"]?([\w-]+)['\"]?\)?",
                         stripped, re.IGNORECASE)
            if m:
                name = m.group(1)
                cics_counts[name] = cics_counts.get(name, 0) + 1

        # structured: valores únicos (keys de cada contador)
        structured = {
            "calls":             list(call_counts.keys()),
            "copybooks":         list(copybook_counts.keys()),
            "files":             list(file_counts.keys()),
            "tables":            list(table_counts.keys()),
            "cics_transactions": list(cics_counts.keys()),
        }

        # search_terms: solo CALLs únicos, sin conteo
        search_terms = ", ".join(call_counts.keys())

        # cleaned_text: todos los campos con conteo
        def format_counts(label, counts):
            if not counts:
                return None
            items = ", ".join(f"{name} ({count})" for name, count in counts.items())
            return f"{label}: {items}"

        cleaned_parts = filter(None, [
            format_counts("CALLS",     call_counts),
            format_counts("COPYBOOKS", copybook_counts),
            format_counts("FILES",     file_counts),
            format_counts("TABLES",    table_counts),
            format_counts("CICS",      cics_counts),
        ])
        cleaned_text = " | ".join(cleaned_parts)

        return func.HttpResponse(
            json.dumps({
                "search_terms": search_terms,
                "structured":   structured,
                "cleaned_text": cleaned_text
            }),
            mimetype="application/json",
            status_code=200
        )

    except Exception as e:
        return func.HttpResponse(f"Error: {str(e)}", status_code=500)


# --- FUNCTION 2: The PDF Converter ---
@app.function_name(name="ConvertTextOrHtmlToPdf")
@app.route("", methods=["POST"])
def ConvertTextOrHtmlToPdf(req: func.HttpRequest) -> func.HttpResponse:
    try:
        # 1. Obtener el JSON de la solicitud
        req_body = req.get_json()
        content = req_body.get('content')
 
        if not content:
            return func.HttpResponse(
                "Error: El campo 'content' está vacío en el JSON.",
                status_code=400
            )
 
        # 2. Convertir a PDF usando fpdf
        pdf_bytes = convert_to_pdf_with_fpdf(content)
        # 3. Retornar el PDF como respuesta binaria
        return func.HttpResponse(
            pdf_bytes,
            mimetype="application/pdf",
            status_code=200
        )
 
    except Exception as e:
        return func.HttpResponse(
            f"Error en la conversión: {str(e)}", 
            status_code=500
        )
 
def convert_to_pdf_with_fpdf(content: str) -> bytes:
    """
    Convierte texto plano o HTML en un PDF válido usando fpdf.
    :param content: Texto plano o HTML
    :return: PDF en bytes
    """
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    # Handle HTML content - simple HTML tag removal
    # Remove HTML tags
    clean_content = re.sub(r'<[^>]+>', '', content)
    # Handle HTML entities
    clean_content = clean_content.replace('&lt;', '<')
    clean_content = clean_content.replace('&gt;', '>')
    clean_content = clean_content.replace('&amp;', '&')
    clean_content = clean_content.replace('&quot;', '"')
    clean_content = clean_content.replace('&#39;', "'")
    # Add content to PDF with word wrap
    pdf.multi_cell(0, 10, txt=clean_content)
    # Get PDF as bytes
    pdf_bytes = pdf.output(dest='S')
    return pdf_bytes

# --- FUNCTION 3: The GetFilesByName / Reemplaza AI Search ---
@app.function_name(name="GetFilesByName")
@app.route("", methods=["POST"])
def GetFilesByName(req: func.HttpRequest) -> func.HttpResponse:
    try:
        req_body = req.get_json()
        search_terms = req_body.get('search_terms', '')
        
        if not search_terms:
            return func.HttpResponse(
                json.dumps({"files": []}),
                mimetype="application/json",
                status_code=200
            )

        # Parsear los nombres
        names = [name.strip() for name in search_terms.split(',') if name.strip()]

        # Connection string desde variables de entorno
        connect_str = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
        blob_service_client = BlobServiceClient.from_connection_string(connect_str)
        container_name = os.environ["AZURE_STORAGE_CONTAINER_NAME"]
        container_client = blob_service_client.get_container_client(container_name)

        results = []

        for name in names:
            found = False

            # Buscar en cob/ primero, luego en pco/
            search_paths = [
                ("cob", f"cob/{name}.COB"),
                ("pco", f"pco/{name}.PCO"),
            ]

            for folder, blob_path in search_paths:
                try:
                    blob_client = container_client.get_blob_client(blob_path)
                    properties = blob_client.get_blob_properties()

                    results.append({
                        "name":          name,
                        "path":          blob_path,
                        "folder":        folder,
                        "extension":     folder.upper(),
                        "size":          properties.size,
                        "last_modified": properties.last_modified.isoformat(),
                        "found":         True
                    })
                    found = True
                    break  # Si lo encontró en cob, no busca en pco

                except ResourceNotFoundError:
                    continue  # Intenta en la siguiente carpeta

            if not found:
                results.append({
                    "name":  name,
                    "found": False
                })

        return func.HttpResponse(
            json.dumps({"files": results}),
            mimetype="application/json",
            status_code=200
        )

    except Exception as e:
        return func.HttpResponse(f"Error: {str(e)}", status_code=500)


# --- FUNCTION 4: CleanComments / Codigo sin comentarios ---
@app.function_name(name="CleanComments")
@app.route("", methods=["POST"])
def CleanComments(req: func.HttpRequest) -> func.HttpResponse:
    try:
        req_body = req.get_json()
        raw_text = req_body.get('text', '')
        lines = raw_text.split('\n')

        total_lines = len(lines)
        removed_lines = 0
        clean_lines = []
        last_code_line = None

        def remove_inline_comment(line):
            in_string = False
            quote_char = None
            i = 0
            while i < len(line):
                char = line[i]
                if char in ('"', "'") and not in_string:
                    in_string = True
                    quote_char = char
                elif char == quote_char and in_string:
                    if i + 1 < len(line) and line[i + 1] == quote_char:
                        i += 2
                        continue
                    else:
                        in_string = False
                        quote_char = None
                elif not in_string and char == '*' \
                        and i + 1 < len(line) and line[i + 1] == '>':
                    return line[:i].strip()
                i += 1
            return line.strip()

        for line in lines:
            # Línea vacía — conservar para estructura visual
            if not line.strip():
                clean_lines.append('')
                continue

            # Obtener indicador en columna 7 (índice 6)
            indicator = line[6] if len(line) > 6 else ' '

            # Comentario con * o /
            if indicator in ('*', '/'):
                removed_lines += 1
                continue

            # Línea de continuación — unir con la línea anterior
            if indicator == '-':
                removed_lines += 1
                # El contenido útil empieza en columna 12 (índice 11)
                continuation_text = line[11:].strip() if len(line) > 11 else ''
                if last_code_line is not None and continuation_text:
                    # Quitar la comilla de cierre del final de la línea anterior
                    # y la comilla de apertura del inicio de la continuación
                    prev = clean_lines[last_code_line]
                    if prev.endswith("'") and continuation_text.startswith("'"):
                        clean_lines[last_code_line] = prev[:-1] + continuation_text[1:]
                    elif prev.endswith('"') and continuation_text.startswith('"'):
                        clean_lines[last_code_line] = prev[:-1] + continuation_text[1:]
                    else:
                        clean_lines[last_code_line] = prev + ' ' + continuation_text
                continue

            # Línea de código normal — limpiar comentario inline
            cleaned = remove_inline_comment(line)
            if cleaned:
                last_code_line = len(clean_lines)
                clean_lines.append(cleaned)
            else:
                removed_lines += 1

        clean_code = '\n'.join(clean_lines)
        clean_line_count = total_lines - removed_lines
        comment_percentage = round((removed_lines / total_lines) * 100, 2) if total_lines > 0 else 0.0

        return func.HttpResponse(
            json.dumps({
                "clean_code": clean_code,
                "metadata": {
                    "total_lines":         total_lines,
                    "removed_lines":       removed_lines,
                    "clean_lines":         clean_line_count,
                    "comment_percentage":  comment_percentage
                }
            }),
            mimetype="application/json",
            status_code=200
        )

    except Exception as e:
        return func.HttpResponse(f"Error: {str(e)}", status_code=500)