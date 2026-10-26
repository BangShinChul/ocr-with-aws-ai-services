import os
import json
import time
import uuid
from datetime import datetime
import numpy as np
from PIL import Image, ImageDraw
from typing import Dict, Any, Tuple, List, Optional

from engines.base import OCREngine
from shared.aws_client import get_aws_client
from shared.image_utils import convert_to_bytes
from shared.config import logger, API_COSTS, POSTPROCESSING_MODEL, S3_BUCKET


class TextractDetectEngine(OCREngine):
    """
    Implementation of OCR engine using Amazon Textract DetectDocumentText API
    """
    
    def __init__(self):
        super().__init__("Textract-Detect")
        
    def process_image(self, image, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Process an image or PDF with Amazon Textract DetectDocumentText API
        """
        options = options or {}
        output_schema = options.get('output_schema')
        s3_bucket = options.get('s3_bucket') or S3_BUCKET
        logger.info(f"[DetectDocumentText] Using S3 bucket: {s3_bucket}")
        
        overall_start_time = time.time()
        timing_ctx = self.get_timing_wrapper()
        
        is_pdf = False
        file_bytes = None
        
        if hasattr(image, 'name') and image.name and image.name.lower().endswith('.pdf'):
            is_pdf = True
            with open(image.name, 'rb') as f:
                file_bytes = f.read()
            if not file_bytes.startswith(b'%PDF-'):
                raise ValueError("Invalid PDF format")
            file_size_mb = len(file_bytes) / (1024 * 1024)
            if file_size_mb > 5:
                raise ValueError("PDF file exceeds 5MB limit")
            img_pil = None
        elif isinstance(image, str) and image.lower().endswith('.pdf'):
            is_pdf = True
            with open(image, 'rb') as f:
                file_bytes = f.read()
            img_pil = None
        else:
            image_bytes, img_pil = convert_to_bytes(image)
            
        # Images go to Textract as raw bytes (synchronous API, up to 10 MB), so no S3 bucket is needed.
        # PDFs still use the asynchronous API, which only accepts S3 input.
        s3_object_key = None
        if is_pdf:
            if not s3_bucket:
                raise ValueError("PDF input needs an S3 bucket. Set OCR_S3_BUCKET (see README) or use an image file.")
            s3_object_key = self._upload_to_s3(file_bytes, s3_bucket, is_pdf)
            self._verify_s3_object(s3_bucket, s3_object_key)
        
        with timing_ctx:
            try:
                textract = get_aws_client('textract')
                
                if is_pdf:
                    operation_type = 'textract_async'
                    response = self._process_pdf_async(textract, s3_bucket, s3_object_key)
                else:
                    operation_type = 'textract_detect'
                    response = textract.detect_document_text(
                        Document={'Bytes': image_bytes}
                    )
                
                # Save output to S3
                # Raw output is archived to S3 only for PDFs (images never touch S3)
                s3_output_key = self._save_output_to_s3(response, s3_bucket, s3_object_key, "detect") if is_pdf else ""
                
                extracted_text = ""
                blocks_count = 0
                total_pages = response.get('DocumentMetadata', {}).get('Pages', 1)
                
                if is_pdf:
                    annotated_image = np.zeros((400, 600, 3), dtype=np.uint8)
                    pages_content = {}
                    
                    for item in response["Blocks"]:
                        page_num = item.get("Page", 1)
                        if page_num not in pages_content:
                            pages_content[page_num] = []
                        if item["BlockType"] == "LINE" and "Text" in item:
                            pages_content[page_num].append(item["Text"])
                            blocks_count += 1
                    
                    for page_num in sorted(pages_content.keys()):
                        extracted_text += f"\n--- Page {page_num} ---\n"
                        extracted_text += "\n".join(pages_content[page_num]) + "\n"
                    
                    from PIL import Image as PILImage, ImageDraw as PILImageDraw
                    pil_img = PILImage.fromarray(annotated_image)
                    draw = PILImageDraw.Draw(pil_img)
                    draw.text((20, 20), f"PDF - DetectDocumentText", fill=(255, 255, 255))
                    draw.text((20, 50), f"Pages: {total_pages}", fill=(255, 255, 255))
                    draw.text((20, 80), f"Blocks: {blocks_count}", fill=(255, 255, 255))
                    annotated_image = np.array(pil_img)
                else:
                    annotated_img = img_pil.copy()
                    draw = ImageDraw.Draw(annotated_img)
                    width, height = annotated_img.size
                    
                    draw.rectangle([(0, 0), (width, height)], outline='#FF0000', width=3)
                    draw.text((20, 20), f"DetectDocumentText ({width}x{height})", fill='#FF0000')
                    
                    for item in response["Blocks"]:
                        if item["BlockType"] == "LINE":
                            extracted_text += item["Text"] + "\n"
                            blocks_count += 1
                            if "Geometry" in item and "BoundingBox" in item["Geometry"]:
                                box = item["Geometry"]["BoundingBox"]
                                left = width * box["Left"]
                                top = height * box["Top"]
                                box_width = width * box["Width"]
                                box_height = height * box["Height"]
                                draw.rectangle(
                                    [(left, top), (left + box_width, top + box_height)],
                                    outline='#FF0000', width=2
                                )
                    annotated_image = np.array(annotated_img)
                
                # LLM processing
                structured_json = None
                token_usage = None
                
                if extracted_text and output_schema:
                    try:
                        from shared.prompt_manager import process_text_with_llm
                        structured_json, token_usage = process_text_with_llm(extracted_text, output_schema)
                    except Exception as llm_error:
                        structured_json = {"error": str(llm_error), "raw_text": extracted_text}
                
                textract_cost = API_COSTS.get(operation_type, 0.0015) * total_pages
                overall_process_time = time.time() - overall_start_time

                return {
                    "text": extracted_text,
                    "json": structured_json,
                    "raw_json": response,
                    "image": annotated_image,
                    "process_time": overall_process_time,
                    "operation_type": operation_type,
                    "pages": total_pages,
                    "blocks_count": blocks_count,
                    "token_usage": token_usage,
                    "textract_cost": textract_cost,
                    "s3_output_key": s3_output_key
                }
                
            except Exception as e:
                logger.error(f"Textract DetectDocumentText Error: {str(e)}")
                return {
                    "text": f"Error: {str(e)}",
                    "json": None,
                    "image": None,
                    "process_time": time.time() - overall_start_time,
                    "operation_type": "error",
                    "pages": 0
                }

    def _upload_to_s3(self, file_bytes: bytes, s3_bucket: str, is_pdf: bool = False) -> str:
        s3_client = get_aws_client('s3')
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        random_id = str(uuid.uuid4())[:8]
        file_extension = "pdf" if is_pdf else "jpg"
        content_type = "application/pdf" if is_pdf else "image/jpeg"
        object_key = f"textract-input/{timestamp}-{random_id}.{file_extension}"
        
        s3_client.put_object(Bucket=s3_bucket, Key=object_key, Body=file_bytes, ContentType=content_type)
        return object_key
    
    def _verify_s3_object(self, s3_bucket: str, s3_object_key: str):
        s3_client = get_aws_client('s3')
        s3_client.head_object(Bucket=s3_bucket, Key=s3_object_key)
    
    def _save_output_to_s3(self, response: Dict, s3_bucket: str, input_key: str, api_type: str) -> str:
        try:
            s3_client = get_aws_client('s3')
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_name = input_key.replace("textract-input/", "").rsplit(".", 1)[0]
            output_key = f"textract-output/{base_name}_{timestamp}_{api_type}_result.json"
            output_json = json.dumps(response, indent=2, default=str)
            s3_client.put_object(Bucket=s3_bucket, Key=output_key, Body=output_json.encode('utf-8'), ContentType='application/json')
            return output_key
        except Exception as e:
            logger.error(f"Failed to save output to S3: {str(e)}")
            return ""
    
    def _process_pdf_async(self, textract_client, s3_bucket: str, s3_object_key: str):
        start_response = textract_client.start_document_text_detection(
            DocumentLocation={'S3Object': {'Bucket': s3_bucket, 'Name': s3_object_key}}
        )
        job_id = start_response['JobId']
        
        max_wait_time = 300
        wait_interval = 2
        total_wait = 0
        
        while total_wait < max_wait_time:
            time.sleep(wait_interval)
            total_wait += wait_interval
            get_response = textract_client.get_document_text_detection(JobId=job_id)
            status = get_response['JobStatus']
            if status == 'SUCCEEDED':
                break
            elif status == 'FAILED':
                raise Exception("Textract job failed")
        
        if total_wait >= max_wait_time:
            raise Exception("Textract job timed out")
        
        all_blocks = []
        pages_metadata = {'Pages': 0}
        next_token = None
        
        while True:
            if next_token:
                get_response = textract_client.get_document_text_detection(JobId=job_id, NextToken=next_token)
            else:
                get_response = textract_client.get_document_text_detection(JobId=job_id)
            all_blocks.extend(get_response.get('Blocks', []))
            if 'DocumentMetadata' in get_response:
                pages_metadata['Pages'] = get_response['DocumentMetadata'].get('Pages', 1)
            next_token = get_response.get('NextToken')
            if not next_token:
                break
        
        return {'DocumentMetadata': pages_metadata, 'Blocks': all_blocks}

    def get_cost(self, result: Dict[str, Any]) -> Tuple[str, float]:
        pages = result.get("pages", 1)
        operation_type = result.get("operation_type", "textract_detect")
        cost_per_page = API_COSTS.get(operation_type, 0.0015)
        textract_base_cost = cost_per_page * pages
        total_cost = textract_base_cost
        token_usage = result.get("token_usage")
        
        if token_usage:
            from shared.cost_calculator import calculate_bedrock_cost
            _, llm_cost = calculate_bedrock_cost(POSTPROCESSING_MODEL, token_usage)
            total_cost += llm_cost
            html = f'<div class="cost-container"><div class="cost-total">${total_cost:.6f}</div></div>'
        else:
            html = f'<div class="cost-container"><div class="cost-total">${total_cost:.6f}</div></div>'
        
        return html, total_cost



class TextractAnalyzeEngine(OCREngine):
    """
    Implementation of OCR engine using Amazon Textract AnalyzeDocument API
    for improved form and table recognition
    """
    
    def __init__(self):
        super().__init__("Textract-Analyze")
        
    def process_image(self, image, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Process an image or PDF with Amazon Textract AnalyzeDocument API
        """
        options = options or {}
        output_schema = options.get('output_schema')
        s3_bucket = options.get('s3_bucket') or S3_BUCKET
        feature_types = options.get('feature_types', ['FORMS', 'TABLES'])
        logger.info(f"[AnalyzeDocument] Using S3 bucket: {s3_bucket}, features: {feature_types}")
        
        overall_start_time = time.time()
        timing_ctx = self.get_timing_wrapper()
        
        is_pdf = False
        file_bytes = None
        
        if hasattr(image, 'name') and image.name and image.name.lower().endswith('.pdf'):
            is_pdf = True
            with open(image.name, 'rb') as f:
                file_bytes = f.read()
            if not file_bytes.startswith(b'%PDF-'):
                raise ValueError("Invalid PDF format")
            file_size_mb = len(file_bytes) / (1024 * 1024)
            if file_size_mb > 5:
                raise ValueError("PDF file exceeds 5MB limit")
            img_pil = None
        elif isinstance(image, str) and image.lower().endswith('.pdf'):
            is_pdf = True
            with open(image, 'rb') as f:
                file_bytes = f.read()
            img_pil = None
        else:
            image_bytes, img_pil = convert_to_bytes(image)
            
        # Images go to Textract as raw bytes (synchronous API, up to 10 MB), so no S3 bucket is needed.
        # PDFs still use the asynchronous API, which only accepts S3 input.
        s3_object_key = None
        if is_pdf:
            if not s3_bucket:
                raise ValueError("PDF input needs an S3 bucket. Set OCR_S3_BUCKET (see README) or use an image file.")
            s3_object_key = self._upload_to_s3(file_bytes, s3_bucket, is_pdf)
            self._verify_s3_object(s3_bucket, s3_object_key)
        
        with timing_ctx:
            try:
                textract = get_aws_client('textract')
                
                if is_pdf:
                    operation_type = 'textract_analyze_async'
                    response = self._process_pdf_async(textract, s3_bucket, s3_object_key, feature_types)
                else:
                    operation_type = 'textract_analyze'
                    response = textract.analyze_document(
                        Document={'Bytes': image_bytes},
                        FeatureTypes=feature_types
                    )
                
                # Save output to S3
                # Raw output is archived to S3 only for PDFs (images never touch S3)
                s3_output_key = self._save_output_to_s3(response, s3_bucket, s3_object_key, "analyze") if is_pdf else ""
                
                extracted_text = ""
                blocks_count = 0
                total_pages = response.get('DocumentMetadata', {}).get('Pages', 1)
                
                forms_data = []
                tables_data = []
                block_map = {block['Id']: block for block in response["Blocks"]}
                
                if is_pdf:
                    annotated_image = np.zeros((400, 600, 3), dtype=np.uint8)
                    pages_content = {}
                    
                    for item in response["Blocks"]:
                        page_num = item.get("Page", 1)
                        if page_num not in pages_content:
                            pages_content[page_num] = []
                        if item["BlockType"] == "LINE" and "Text" in item:
                            pages_content[page_num].append(item["Text"])
                            blocks_count += 1
                        elif item["BlockType"] == "KEY_VALUE_SET":
                            form_item = self._extract_key_value_pair(item, block_map)
                            if form_item:
                                forms_data.append(form_item)
                        elif item["BlockType"] == "TABLE":
                            table_item = self._extract_table(item, block_map)
                            if table_item:
                                tables_data.append(table_item)
                    
                    for page_num in sorted(pages_content.keys()):
                        extracted_text += f"\n--- Page {page_num} ---\n"
                        extracted_text += "\n".join(pages_content[page_num]) + "\n"
                    
                    from PIL import Image as PILImage, ImageDraw as PILImageDraw
                    pil_img = PILImage.fromarray(annotated_image)
                    draw = PILImageDraw.Draw(pil_img)
                    draw.text((20, 20), f"PDF - AnalyzeDocument", fill=(255, 255, 255))
                    draw.text((20, 50), f"Pages: {total_pages}", fill=(255, 255, 255))
                    draw.text((20, 80), f"Forms: {len(forms_data)}, Tables: {len(tables_data)}", fill=(255, 255, 255))
                    annotated_image = np.array(pil_img)
                else:
                    annotated_img = img_pil.copy()
                    draw = ImageDraw.Draw(annotated_img)
                    width, height = annotated_img.size
                    
                    draw.rectangle([(0, 0), (width, height)], outline='#00AA00', width=3)
                    draw.text((20, 20), f"AnalyzeDocument ({width}x{height})", fill='#00AA00')
                    
                    for item in response["Blocks"]:
                        if item["BlockType"] == "LINE":
                            extracted_text += item["Text"] + "\n"
                            blocks_count += 1
                            if "Geometry" in item and "BoundingBox" in item["Geometry"]:
                                box = item["Geometry"]["BoundingBox"]
                                left = width * box["Left"]
                                top = height * box["Top"]
                                box_width = width * box["Width"]
                                box_height = height * box["Height"]
                                draw.rectangle(
                                    [(left, top), (left + box_width, top + box_height)],
                                    outline='#FF0000', width=2
                                )
                        elif item["BlockType"] == "KEY_VALUE_SET":
                            form_item = self._extract_key_value_pair(item, block_map)
                            if form_item:
                                forms_data.append(form_item)
                                if "Geometry" in item and "BoundingBox" in item["Geometry"]:
                                    box = item["Geometry"]["BoundingBox"]
                                    left = width * box["Left"]
                                    top = height * box["Top"]
                                    box_width = width * box["Width"]
                                    box_height = height * box["Height"]
                                    draw.rectangle(
                                        [(left, top), (left + box_width, top + box_height)],
                                        outline='#00FF00', width=2
                                    )
                        elif item["BlockType"] == "TABLE":
                            table_item = self._extract_table(item, block_map)
                            if table_item:
                                tables_data.append(table_item)
                                if "Geometry" in item and "BoundingBox" in item["Geometry"]:
                                    box = item["Geometry"]["BoundingBox"]
                                    left = width * box["Left"]
                                    top = height * box["Top"]
                                    box_width = width * box["Width"]
                                    box_height = height * box["Height"]
                                    draw.rectangle(
                                        [(left, top), (left + box_width, top + box_height)],
                                        outline='#0000FF', width=2
                                    )
                    annotated_image = np.array(annotated_img)
                
                structured_output = {
                    "text": extracted_text,
                    "forms": forms_data,
                    "tables": tables_data
                }
                
                # LLM processing
                structured_json = None
                token_usage = None
                
                if extracted_text and output_schema:
                    try:
                        from shared.prompt_manager import process_text_with_llm
                        enhanced_text = extracted_text
                        if forms_data:
                            enhanced_text += "\n\n--- Form Fields ---\n"
                            for form in forms_data:
                                enhanced_text += f"{form.get('key', '')}: {form.get('value', '')}\n"
                        if tables_data:
                            enhanced_text += "\n\n--- Tables ---\n"
                            for idx, table in enumerate(tables_data):
                                enhanced_text += f"Table {idx + 1}:\n{json.dumps(table, indent=2)}\n"
                        structured_json, token_usage = process_text_with_llm(enhanced_text, output_schema)
                    except Exception as llm_error:
                        structured_json = {"error": str(llm_error), "raw_text": extracted_text}
                
                textract_cost = API_COSTS.get(operation_type, 0.065) * total_pages
                overall_process_time = time.time() - overall_start_time

                return {
                    "text": extracted_text,
                    "json": structured_json,
                    "raw_json": response,
                    "structured_output": structured_output,
                    "image": annotated_image,
                    "process_time": overall_process_time,
                    "operation_type": operation_type,
                    "pages": total_pages,
                    "blocks_count": blocks_count,
                    "forms_count": len(forms_data),
                    "tables_count": len(tables_data),
                    "token_usage": token_usage,
                    "textract_cost": textract_cost,
                    "s3_output_key": s3_output_key
                }
                
            except Exception as e:
                logger.error(f"Textract AnalyzeDocument Error: {str(e)}")
                return {
                    "text": f"Error: {str(e)}",
                    "json": None,
                    "image": None,
                    "process_time": time.time() - overall_start_time,
                    "operation_type": "error",
                    "pages": 0
                }

    def _extract_key_value_pair(self, block: Dict, block_map: Dict) -> Optional[Dict]:
        if block.get("EntityTypes") != ["KEY"]:
            return None
        key_text = ""
        value_text = ""
        if "Relationships" in block:
            for rel in block["Relationships"]:
                if rel["Type"] == "CHILD":
                    for child_id in rel["Ids"]:
                        child_block = block_map.get(child_id, {})
                        if "Text" in child_block:
                            key_text += child_block["Text"] + " "
                elif rel["Type"] == "VALUE":
                    for value_id in rel["Ids"]:
                        value_block = block_map.get(value_id, {})
                        if "Relationships" in value_block:
                            for value_rel in value_block["Relationships"]:
                                if value_rel["Type"] == "CHILD":
                                    for child_id in value_rel["Ids"]:
                                        child_block = block_map.get(child_id, {})
                                        if "Text" in child_block:
                                            value_text += child_block["Text"] + " "
        key_text = key_text.strip()
        value_text = value_text.strip()
        if key_text:
            return {"key": key_text, "value": value_text, "confidence": block.get("Confidence", 0)}
        return None
    
    def _extract_table(self, block: Dict, block_map: Dict) -> Optional[Dict]:
        if block["BlockType"] != "TABLE":
            return None
        rows = {}
        if "Relationships" in block:
            for rel in block["Relationships"]:
                if rel["Type"] == "CHILD":
                    for cell_id in rel["Ids"]:
                        cell_block = block_map.get(cell_id, {})
                        if cell_block.get("BlockType") == "CELL":
                            row_idx = cell_block.get("RowIndex", 0)
                            col_idx = cell_block.get("ColumnIndex", 0)
                            cell_text = ""
                            if "Relationships" in cell_block:
                                for cell_rel in cell_block["Relationships"]:
                                    if cell_rel["Type"] == "CHILD":
                                        for word_id in cell_rel["Ids"]:
                                            word_block = block_map.get(word_id, {})
                                            if "Text" in word_block:
                                                cell_text += word_block["Text"] + " "
                            if row_idx not in rows:
                                rows[row_idx] = {}
                            rows[row_idx][col_idx] = cell_text.strip()
        if rows:
            max_row = max(rows.keys())
            max_col = max(max(cols.keys()) for cols in rows.values()) if rows else 0
            table_data = []
            for r in range(1, max_row + 1):
                row_data = []
                for c in range(1, max_col + 1):
                    row_data.append(rows.get(r, {}).get(c, ""))
                table_data.append(row_data)
            return {"rows": table_data, "row_count": max_row, "column_count": max_col, "confidence": block.get("Confidence", 0)}
        return None

    def _upload_to_s3(self, file_bytes: bytes, s3_bucket: str, is_pdf: bool = False) -> str:
        s3_client = get_aws_client('s3')
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        random_id = str(uuid.uuid4())[:8]
        file_extension = "pdf" if is_pdf else "jpg"
        content_type = "application/pdf" if is_pdf else "image/jpeg"
        object_key = f"textract-input/{timestamp}-{random_id}.{file_extension}"
        s3_client.put_object(Bucket=s3_bucket, Key=object_key, Body=file_bytes, ContentType=content_type)
        return object_key
    
    def _verify_s3_object(self, s3_bucket: str, s3_object_key: str):
        s3_client = get_aws_client('s3')
        s3_client.head_object(Bucket=s3_bucket, Key=s3_object_key)
    
    def _save_output_to_s3(self, response: Dict, s3_bucket: str, input_key: str, api_type: str) -> str:
        try:
            s3_client = get_aws_client('s3')
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_name = input_key.replace("textract-input/", "").rsplit(".", 1)[0]
            output_key = f"textract-output/{base_name}_{timestamp}_{api_type}_result.json"
            output_json = json.dumps(response, indent=2, default=str)
            s3_client.put_object(Bucket=s3_bucket, Key=output_key, Body=output_json.encode('utf-8'), ContentType='application/json')
            return output_key
        except Exception as e:
            logger.error(f"Failed to save output to S3: {str(e)}")
            return ""
    
    def _process_pdf_async(self, textract_client, s3_bucket: str, s3_object_key: str, feature_types: List[str]):
        start_response = textract_client.start_document_analysis(
            DocumentLocation={'S3Object': {'Bucket': s3_bucket, 'Name': s3_object_key}},
            FeatureTypes=feature_types
        )
        job_id = start_response['JobId']
        
        max_wait_time = 300
        wait_interval = 2
        total_wait = 0
        
        while total_wait < max_wait_time:
            time.sleep(wait_interval)
            total_wait += wait_interval
            get_response = textract_client.get_document_analysis(JobId=job_id)
            status = get_response['JobStatus']
            if status == 'SUCCEEDED':
                break
            elif status == 'FAILED':
                raise Exception("Textract job failed")
        
        if total_wait >= max_wait_time:
            raise Exception("Textract job timed out")
        
        all_blocks = []
        pages_metadata = {'Pages': 0}
        next_token = None
        
        while True:
            if next_token:
                get_response = textract_client.get_document_analysis(JobId=job_id, NextToken=next_token)
            else:
                get_response = textract_client.get_document_analysis(JobId=job_id)
            all_blocks.extend(get_response.get('Blocks', []))
            if 'DocumentMetadata' in get_response:
                pages_metadata['Pages'] = get_response['DocumentMetadata'].get('Pages', 1)
            next_token = get_response.get('NextToken')
            if not next_token:
                break
        
        return {'DocumentMetadata': pages_metadata, 'Blocks': all_blocks}

    def get_cost(self, result: Dict[str, Any]) -> Tuple[str, float]:
        pages = result.get("pages", 1)
        operation_type = result.get("operation_type", "textract_analyze")
        cost_per_page = API_COSTS.get(operation_type, 0.065)
        textract_base_cost = cost_per_page * pages
        total_cost = textract_base_cost
        token_usage = result.get("token_usage")
        
        if token_usage:
            from shared.cost_calculator import calculate_bedrock_cost
            _, llm_cost = calculate_bedrock_cost(POSTPROCESSING_MODEL, token_usage)
            total_cost += llm_cost
        
        html = f'<div class="cost-container"><div class="cost-total">${total_cost:.6f}</div></div>'
        return html, total_cost


# Backward compatibility alias
class TextractEngine(TextractAnalyzeEngine):
    """Alias for backward compatibility - defaults to AnalyzeDocument API"""
    def __init__(self):
        super().__init__()
        self.name = "Textract"
