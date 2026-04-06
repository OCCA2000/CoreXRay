import azure.functions as func
import json

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

@app.function_name(name="CleanData")
@app.http_trigger(arg_name="req", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def CleanData(req: func.HttpRequest) -> func.HttpResponse:
    try:
        # Get the text from the Logic App
        req_body = req.get_json()
        raw_text = req_body.get('text', '')

        # THE CLEANER: 
        # 1. Split into lines
        # 2. Removes first 6 chars (COBOL line numbers)
        # 3. Removes trailing spaces and ignores empty lines
        lines = raw_text.split('\n')
        cleaned_lines = [line[6:].rstrip() for line in lines if len(line.strip()) > 6]
        
        cleaned_text = "\n".join(cleaned_lines)

        return func.HttpResponse(
            json.dumps({"cleaned_text": cleaned_text}),
            mimetype="application/json",
            status_code=200
        )
    except Exception as e:
        return func.HttpResponse(f"Error: {str(e)}", status_code=500)