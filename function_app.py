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

        structured = {
            "calls": [],
            "copybooks": [],
            "files": [],
            "tables": [],
            "cics_transactions": [],
        }
        search_terms = set()

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

            # FROM tabla (ignora subqueries y keywords)
            for m in re.finditer(r'\bFROM\s+([\w-]+)', sql, re.IGNORECASE):
                name = m.group(1)
                if name.upper() not in ('SELECT', 'WHERE', 'SET', '('):
                    tables.add(name)

            # JOIN tabla
            for m in re.finditer(
                r'\b(?:INNER|LEFT|RIGHT|FULL|CROSS)?\s*'
                r'(?:OUTER\s+)?JOIN\s+([\w-]+)',
                sql, re.IGNORECASE
            ):
                tables.add(m.group(1))

            # INSERT INTO tabla
            for m in re.finditer(
                r'\bINSERT\s+INTO\s+([\w-]+)', sql, re.IGNORECASE
            ):
                tables.add(m.group(1))

            # UPDATE tabla
            for m in re.finditer(
                r'\bUPDATE\s+([\w-]+)', sql, re.IGNORECASE
            ):
                tables.add(m.group(1))

            # DELETE FROM tabla
            for m in re.finditer(
                r'\bDELETE\s+FROM\s+([\w-]+)', sql, re.IGNORECASE
            ):
                tables.add(m.group(1))

            return tables

        # --- Procesamiento de líneas ---
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

            # Detectar inicio de bloque SQL
            if re.search(r'\bEXEC\s+SQL\b', stripped, re.IGNORECASE):
                in_sql_block = True
                sql_block = []
                continue

            # Detectar fin de bloque SQL y procesar
            if re.search(r'\bEND-EXEC\b', stripped, re.IGNORECASE):
                if in_sql_block and sql_block:
                    full_sql = ' '.join(sql_block)
                    found_tables = extract_tables_from_sql(full_sql)
                    structured["tables"].extend(found_tables)
                    # ← NO se agrega a search_terms (decisión anterior)
                in_sql_block = False
                sql_block = []
                continue

            # Acumular líneas dentro de bloque SQL
            if in_sql_block:
                sql_block.append(stripped)
                continue  # ← no procesar otras keywords dentro de SQL

            # --- Fuera de bloque SQL ---

            # CALL
            m = re.search(r"CALL\s+['\"]?([\w-]+)['\"]?",
                         stripped, re.IGNORECASE)
            if m:
                structured["calls"].append(m.group(1))
                search_terms.add(m.group(1))

            # COPY
            m = re.search(r"COPY\s+([\w-]+)",
                         stripped, re.IGNORECASE)
            if m:
                structured["copybooks"].append(m.group(1))
                search_terms.add(m.group(1))

            # FD
            m = re.search(r"\bFD\s+([\w-]+)",
                         stripped, re.IGNORECASE)
            if m:
                file_name = m.group(1)
                for suffix in file_suffixes:
                    if file_name.upper().endswith(suffix.upper()):
                        file_name = file_name[:len(file_name)-len(suffix)]
                        break
                structured["files"].append(file_name)
                search_terms.add(file_name)

            # TRANSID
            m = re.search(r"TRANSID\s*\(?['\"]?([\w-]+)['\"]?\)?",
                         stripped, re.IGNORECASE)
            if m:
                structured["cics_transactions"].append(m.group(1))
                search_terms.add(m.group(1))

        return func.HttpResponse(
            json.dumps({
                "search_terms": ", ".join(search_terms),
                "structured":   structured,
                "cleaned_text": ", ".join(search_terms)
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