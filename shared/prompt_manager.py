
# prompt_manager.py
import json
import time
import logging

from .aws_client import get_aws_client
from .config import POSTPROCESSING_MODEL, logger

from .prompts import (  # noqa: F401  (re-exported for existing imports)
    OCR_SYSTEM_PROMPT, DOCUMENT_TYPE_PROMPT_MAP, JSON_SYSTEM_PROMPT, JSON_TEMPLATE_NO_SCHEMA,
    get_json_formatting_instructions, get_prompt_for_document_type,
)

def process_text_with_llm(text, output_schema=None):
    """
    Process extracted text with LLM to structure it as JSON
    
    Args:
        text: Extracted text to process
        output_schema: Optional JSON schema to conform to
        
    Returns:
        Tuple of (structured JSON, token usage)
    """
    # Start timing
    start_time = time.time()
    
    # Create Bedrock Runtime client
    bedrock_runtime = get_aws_client('bedrock-runtime')
    
    try:
        # Prepare the prompt for JSON conversion
        prompt = f"""Please convert the following extracted text into structured JSON format:

{text}
"""
        if output_schema:
            prompt += f"\n\nPlease format the output according to this JSON schema: {output_schema}"
            prompt += "\nIMPORTANT: Return ONLY the JSON data without any markdown code blocks, backticks or formatting. Ensure all quotes and special characters are properly escaped."
        else:
            prompt += "\n\n" + JSON_TEMPLATE_NO_SCHEMA
        
        messages = [{"role": "user", "content": [{"text": prompt}]}]
        system_messages = [{"text": JSON_SYSTEM_PROMPT}]
        
        logger.info(f"Calling Bedrock with model: {POSTPROCESSING_MODEL}")
        
        response = bedrock_runtime.converse(
            modelId=POSTPROCESSING_MODEL,
            messages=messages,
            system=system_messages
        )
        
        # Extract text and token usage
        structured_text = ""
        token_usage = {
            'inputTokens': response.get('usage', {}).get('inputTokens', 0),
            'outputTokens': response.get('usage', {}).get('outputTokens', 0),
            'totalTokens': response.get('usage', {}).get('totalTokens', 0)
        }
        
        logger.info(f"Token usage - Input: {token_usage['inputTokens']}, Output: {token_usage['outputTokens']}")
        
        if 'output' in response and 'message' in response['output']:
            message = response['output']['message']
            if 'content' in message:
                for content_item in message['content']:
                    if 'text' in content_item:
                        text = content_item['text'].strip()
                        if text.startswith("```json"):
                            text = text[7:]
                        elif text.startswith("```"):
                            text = text[3:]
                        if text.endswith("```"):
                            text = text[:-3]
                        structured_text += text.strip()
        
        try:
            structured_json = json.loads(structured_text)
            json_process_time = time.time() - start_time
            logger.info(f"JSON conversion completed in {json_process_time:.2f} seconds")
            return structured_json, token_usage
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON: {str(e)}")
            
            error_pos = e.pos
            context_range = 20
            start_pos = max(0, error_pos - context_range)
            end_pos = min(len(structured_text), error_pos + context_range)
            error_context = structured_text[start_pos:end_pos]
            logger.error(f"Context around error: '...{error_context}...'")
            
            json_process_time = time.time() - start_time
            logger.info(f"JSON conversion failed in {json_process_time:.2f} seconds")
            return {"error": "Failed to parse JSON", "raw_text": structured_text}, token_usage
            
    except Exception as e:
        logger.error(f"Error in LLM processing: {str(e)}")
        json_process_time = time.time() - start_time
        logger.info(f"JSON conversion failed in {json_process_time:.2f} seconds")
        raise
