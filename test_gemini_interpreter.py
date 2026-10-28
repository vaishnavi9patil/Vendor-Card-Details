
import json
import time

from ai_interpreter import interpret_vendor_text

sample_text = """
Sunrise World Travel DMC PTE LTD
MD. PARVEJ
Sales & Manager
Singapore +65 86720697
India +91 95997 78257
query@sunrisewtdmc.com
www.sunrisewtdmc.com
Blk 240 Pasir Ris St 21, #06-45
Singapore (510240)
DESTINATION MANAGEMENT COMPANY
"""

start = time.time()

result = interpret_vendor_text(sample_text)

print("Time taken:", round(time.time() - start, 1), "seconds")
print(json.dumps(result, indent=2, ensure_ascii=False))
