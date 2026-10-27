from .config import API_COSTS, POSTPROCESSING_MODEL

def bedrock_cost_breakdown(model_id, token_usage):
    """
    Cost of one Bedrock call from the token counts in the API response.

    cost = input x input price + cache write x cache-write price + cache read x cache-read price
           + output x output price

    Returns a dict with the token counts, the billed cost, and the cost the same call would have
    without any cache hit ("uncached"): useful when the same test image is sent repeatedly, which
    makes OpenAI models bill most of the prompt at the cache-read price. Returns None when the
    model has no price entry.
    """
    prices = API_COSTS.get('bedrock', {}).get(model_id)
    if not token_usage or not prices:
        return None
    tin = token_usage.get('inputTokens', 0) or 0
    tout = token_usage.get('outputTokens', 0) or 0
    tcw = token_usage.get('cacheWriteInputTokens', 0) or 0
    tcr = token_usage.get('cacheReadInputTokens', 0) or 0
    billed = (tin * prices['input'] + tcw * prices['cache_write'] + tcr * prices['cache_read']
              + tout * prices['output']) / 1000
    # Without a cache hit the cached prefix is written again (OpenAI) or read as plain input.
    first_rate = prices['cache_write'] if (tcw or tcr) else prices['input']
    uncached = (tin * prices['input'] + (tcw + tcr) * first_rate + tout * prices['output']) / 1000
    return {"input_tokens": tin, "cache_write_tokens": tcw, "cache_read_tokens": tcr,
            "output_tokens": tout, "cost": billed, "cost_uncached": uncached, "prices_per_1k": prices}


def calculate_bedrock_cost(model_id, token_usage):
    """
    Calculate the cost of a Bedrock API call

    Returns:
        Tuple of (HTML representation of cost, billed cost value)
    """
    b = bedrock_cost_breakdown(model_id, token_usage)
    if b is None:
        return '<div class="cost-none">No cost data available for this model</div>', 0.0
    p = b["prices_per_1k"]
    cache_line = ""
    if b["cache_write_tokens"] or b["cache_read_tokens"]:
        cache_line = (f'<br><span>{b["cache_write_tokens"]} cache-write tokens (${p["cache_write"]:.6f}/1K), '
                      f'{b["cache_read_tokens"]} cache-read tokens (${p["cache_read"]:.6f}/1K). '
                      f'Without a cache hit: ${b["cost_uncached"]:.6f}</span>')
    html = f'''
    <div class="cost-container">
        <div class="cost-total">${b["cost"]:.6f} total</div>
        <div class="cost-breakdown">
            <span>{b["input_tokens"]} input tokens (${p["input"]:.6f}/1K tokens)</span><br>
            <span>{b["output_tokens"]} output tokens (${p["output"]:.6f}/1K tokens)</span>{cache_line}
        </div>
    </div>
    '''
    return html, b["cost"]


def calculate_textract_cost(operation_type='textract_detect', page_count=1):
    """
    Calculate the cost of a Textract API call
    
    Args:
        operation_type: The Textract operation type ('textract_detect' or 'textract_analyze_tables_forms')
        page_count: Number of pages processed
        
    Returns:
        Tuple: (HTML string with cost information, cost value)
    """
    if operation_type not in API_COSTS:
        return '<div class="cost-none">No cost data available for this operation</div>', 0.0
    
    cost_per_page = API_COSTS[operation_type]
    total_cost = cost_per_page * page_count
    
    # Format HTML output
    html = f'''
    <div class="cost-container">
        <div class="cost-total">${total_cost:.6f} total</div>
        <div class="cost-breakdown">
            <span>${cost_per_page:.6f} per page \u00d7 {page_count} pages</span>
        </div>
    </div>
    '''
    
    return html, total_cost

def calculate_full_textract_cost(result):
    """
    Calculate the total cost including LLM processing
    
    Args:
        result: Result dictionary from Textract processing
        
    Returns:
        Total cost value
    """
    pages = result.get("pages", 1)
    operation_type = result.get("operation_type", "textract_detect")
    
    # Get base textract cost
    _, textract_base_cost = calculate_textract_cost(operation_type, pages)
    
    # Add LLM cost if applicable
    total_cost = textract_base_cost
    token_usage = result.get("token_usage")
    if token_usage:
        _, llm_cost = calculate_bedrock_cost(POSTPROCESSING_MODEL, token_usage)
        total_cost += llm_cost
    
    return total_cost

def calculate_bda_cost(use_blueprint, document_type, page_count=1, field_count=0):
    """
    Calculate the cost of a BDA API call
    
    Args:
        use_blueprint: Whether Custom Output (Blueprint) was used
        document_type: 'document' or 'image'
        page_count: Number of pages/images processed
        field_count: Number of fields defined in blueprint (only relevant for custom output)
        
    Returns:
        HTML string with cost information, and the actual cost value
    """
    if document_type not in ['document', 'image']:
        document_type = 'document'  # Default to document type
        
    if use_blueprint:
        # Custom Output with blueprint
        cost_per_unit = API_COSTS['bda']['custom'][document_type]
        
        # Calculate additional cost for extra fields beyond 30
        extra_field_cost = 0
        if field_count > 30:
            extra_field_cost = API_COSTS['bda']['custom']['extra_field'] * (field_count - 30) * page_count
            
        total_cost = (cost_per_unit * page_count) + extra_field_cost
        
        # Format HTML output
        html = f'''
        <div class="cost-container">
            <div class="cost-total">${total_cost:.6f} total</div>
            <div class="cost-breakdown">
                <span>${cost_per_unit:.6f} per {document_type} \u00d7 {page_count} {document_type}s</span>
                {f"<br><span>${extra_field_cost:.6f} for additional {field_count-30} fields</span>" if field_count > 30 else ""}
            </div>
        </div>
        '''
    else:
        # Standard Output
        cost_per_unit = API_COSTS['bda']['standard'][document_type]
        total_cost = cost_per_unit * page_count
        
        # Format HTML output
        html = f'''
        <div class="cost-container">
            <div class="cost-total">${total_cost:.6f} total</div>
            <div class="cost-breakdown">
                <span>${cost_per_unit:.6f} per {document_type} \u00d7 {page_count} {document_type}s</span>
            </div>
        </div>
        '''
    
    return html, total_cost