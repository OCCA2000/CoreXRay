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

        # Sufijos a remover de nombres de archivos COBOL
        file_suffixes = [
            '-FILE-IN', '-FILE-OUT',  # primero los más específicos
            '-ARCHIVO-IN', '-ARCHIVO-OUT',
            '-FILE', '-ARCHIVO',
            '-INPUT', '-OUTPUT',
            '-IN', '-OUT'
        ]

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith('*'):
                continue

            # CALL → nombre del programa (con guiones)
            m = re.search(r"CALL\s+['\"]?([\w-]+)['\"]?", stripped, re.IGNORECASE)
            if m:
                structured["calls"].append(m.group(1))
                search_terms.add(m.group(1))

            # COPY → nombre completo del copybook (con guiones)
            m = re.search(r"COPY\s+([\w-]+)", stripped, re.IGNORECASE)
            if m:
                structured["copybooks"].append(m.group(1))
                search_terms.add(m.group(1))

            # FD → nombre limpio sin sufijos de archivo
            m = re.search(r"\bFD\s+([\w-]+)", stripped, re.IGNORECASE)
            if m:
                file_name = m.group(1)
                for suffix in file_suffixes:
                    if file_name.upper().endswith(suffix.upper()):
                        file_name = file_name[:len(file_name)-len(suffix)]
                        break
                structured["files"].append(file_name)
                search_terms.add(file_name)

            # CICS TRANSID → ID de transacción
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
from weasyprint import HTML
import azure.functions as func
 
@app.function_name(name="ConvertTextOrHtmlToPdf")
@app.route("", methods=["POST"])
def ConvertTextOrHtmlToPdf(req: func.HttpRequest) -> func.HttpResponse:
    try:
        req_body = req.get_json()
        content = req_body.get("content")
 
        if not content:
            return func.HttpResponse(
                "Error: El campo 'content' está vacío.",
                status_code=400
            )
 
        pdf_bytes = convert_html_to_pdf(content)
 
        return func.HttpResponse(
            body=pdf_bytes,
            mimetype="application/pdf",
            status_code=200
        )
 
    except Exception as e:
        return func.HttpResponse(
            f"Error en la conversión: {str(e)}",
            status_code=500
        )
 
 
def convert_html_to_pdf(html_content: str) -> bytes:
    pdf_bytes = HTML(string=html_content).write_pdf()
    return pdf_bytes
