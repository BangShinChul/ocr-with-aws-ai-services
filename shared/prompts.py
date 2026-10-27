"""Prompt text shared by the local demo and the deployed pipeline (no third-party imports)."""

OCR_SYSTEM_PROMPT = """
You are an expert OCR and document analysis system. Extract all text and information from the image 
with high accuracy. Organize the results clearly and preserve the structure of the document. 
Pay special attention to tables, forms, and formatted data. Use the provided schema to structure 
your response if one is specified.
"""

DOCUMENT_TYPE_PROMPT_MAP = {
    "generic": """
Extract all the text from the provided image. Preserve the original structure and formatting as much as possible.
For tables, maintain the rows and columns relationships. For forms, associate labels with their corresponding values.
For multi-column layouts, process each column in order from left to right.
""",
    "form": """
This image contains a form or receipt. Please extract all fields and their values, preserving the 
relationships between labels and data. Format the output as key-value pairs.
""",
    "receipt": """
This image contains a form or receipt. Please extract all fields and their values, preserving the 
relationships between labels and data. Format the output as key-value pairs.
""",
    "table": """
This image contains tabular data. Extract the complete table including headers and all cell values.
Preserve the row and column structure precisely.
""",
    "handwritten": """
This image contains handwritten text. Try to extract all the handwritten content with high accuracy.
If parts are illegible, indicate that with [illegible].
"""
}

JSON_SYSTEM_PROMPT = """
You are an AI assistant specialized in structuring extracted document text into JSON format.
Your task is to analyze the document content and create the most appropriate JSON structure.
Focus on capturing all relevant information in a logical hierarchy.
Ensure the JSON is valid and represents the information accurately.
IMPORTANT: When returning JSON, do not use code blocks, backticks or markdown formatting.
"""

JSON_TEMPLATE_NO_SCHEMA = """
Please analyze the content and create an appropriate JSON schema based on the document type.
Follow these guidelines:

1. If it's an invoice or receipt:
   - Extract date, invoice number, total amount, tax, vendor details
   - Itemize line items with quantity, description, unit price and subtotal
   
2. If it's a form or application:
   - Identify all field names and their corresponding values
   - Group related fields into logical sections
   
3. If it's a table:
   - Preserve the tabular structure with rows and columns
   - Include column headers as keys
   
4. If it's an ID or card:
   - Extract all personal information fields
   - Include issuer, ID number, date fields, and other relevant data
   
5. For general documents:
   - Extract titles, subtitles, and section headers
   - Organize content into a logical hierarchy
   - Include metadata such as dates, reference numbers, etc.

IMPORTANT: Return ONLY the JSON data without any markdown code blocks, backticks or formatting. Ensure the output is valid JSON.
"""


def get_json_formatting_instructions(output_schema=None):
    """
    Get JSON formatting instructions based on whether a schema is provided
    
    Args:
        output_schema: Optional JSON schema to conform to
        
    Returns:
        str: The appropriate JSON formatting instructions
    """
    if output_schema:
        return f"\n\nPlease format the output according to this JSON schema: {output_schema}\nIMPORTANT: Return ONLY the JSON data without any markdown code blocks, backticks or formatting. Ensure all quotes and special characters are properly escaped."
    else:
        return "\n\n" + JSON_TEMPLATE_NO_SCHEMA

def get_prompt_for_document_type(document_type="generic"):
    """
    Get appropriate user prompt based on document type
    
    Args:
        document_type: Type of document (generic, form, receipt, table, handwritten)
        
    Returns:
        str: The appropriate user prompt
    """
    return DOCUMENT_TYPE_PROMPT_MAP.get(document_type.lower(), DOCUMENT_TYPE_PROMPT_MAP["generic"])
