#!/usr/bin/env python3
import pypdf
import re

def main():
    pdf_path = "/home/administrator/starfall/data/Anime_BluRay_Batch_Sourcing.pdf"
    reader = pypdf.PdfReader(pdf_path)

    # Let's inspect pages where My Hero Academia, Attack on Titan, and Fire Force are
    keywords = ["My Hero Academia", "Attack on Titan", "Fire Force"]
    
    with open("/home/administrator/starfall/data/target_anime_pages.txt", "w", encoding="utf-8") as out:
        for idx, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            # remove spaces that break URLs like 'https://nyaa .si/view/123 456'
            # First let's write raw text
            matched = [k for k in keywords if k.lower() in re.sub(r'\s+', ' ', text).lower()]
            if matched:
                out.write(f"\n{'='*30} PAGE {idx+1}: {', '.join(matched)} {'='*30}\n")
                out.write(text + "\n")

    print("Target pages written to /home/administrator/starfall/data/target_anime_pages.txt")

if __name__ == "__main__":
    main()
