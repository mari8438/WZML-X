#!/usr/bin/env python3
import pypdf
import re

reader = pypdf.PdfReader('/home/administrator/starfall/data/Anime_BluRay_Batch_Sourcing.pdf')

target_pages = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 35, 36, 37]

for pno in target_pages:
    page = reader.pages[pno - 1]
    raw = page.extract_text() or ""
    # Normalize multiple whitespace/newlines into a single space
    clean = re.sub(r'\s+', ' ', raw)
    print(f"\n{'='*20} PAGE {pno} {'='*20}")
    print(clean)
