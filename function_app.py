import azure.functions as func
import json
import io
import re
from fpdf import FPDF

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