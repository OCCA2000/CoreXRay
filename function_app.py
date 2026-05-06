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

            # FD → nombre completo del archivo (con guiones)
            m = re.search(r"\bFD\s+([\w-]+)", stripped, re.IGNORECASE)
            if m:
                structured["files"].append(m.group(1))
                search_terms.add(m.group(1))

            # FROM → nombre completo de tabla (con guiones)
            #m = re.search(r"\bFROM\s+([\w-]+)", stripped, re.IGNORECASE)
            #if m:
                #structured["tables"].append(m.group(1))
                #search_terms.add(m.group(1))

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