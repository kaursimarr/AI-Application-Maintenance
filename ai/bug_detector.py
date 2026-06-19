import os
import uuid
from datetime import datetime
import json
from ai.groq_client import get_client

def detect_bugs():

    client = get_client()

    file_path = "templates/emi_calculator.html"

    with open(file_path, "r", encoding="utf-8") as f:
        code = f.read()

    prompt = f"""
Analyze this HTML code.

Find real bugs, validation issues,
UI problems and logic errors.

Return ONLY JSON:

{{
    "bugs":[
        {{
            "title":"bug title",
            "description":"bug description"
        }}
    ]
}}

CODE:
{code}
"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {
                "role":"user",
                "content":prompt
            }
        ],
        response_format={
            "type":"json_object"
        }
    )

    result = json.loads(
        response.choices[0].message.content
    )

    bugs = []

    for bug in result.get("bugs", []):

        bugs.append({
            "id": str(uuid.uuid4())[:8],
            "title": bug["title"],
            "description": bug["description"],
            "status": "AI Detected",
            "source": "AI",
            "createdOn":
            datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
            "ai_remark":
            "Detected automatically by Groq"
        })

    return bugs