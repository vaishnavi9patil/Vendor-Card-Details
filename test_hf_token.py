
import os
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("HF_TOKEN")

if token and token.startswith("hf_"):
    print("HF_TOKEN found. Token is not displayed.")
else:
    print("HF_TOKEN missing or does not look correct.")
