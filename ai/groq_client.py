import os
from groq import Groq
from dotenv import load_dotenv

# Load the variables from the .env file into your environment
load_dotenv()
# os.getenv automatically looks for the variable name you defined inside the .env file
def get_client():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY not found in .env file.")
    return Groq(api_key=api_key)